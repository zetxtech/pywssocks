from http import HTTPStatus
from typing import Iterable, Optional, Tuple, Union
import logging
import asyncio
import json
import socket
import random
import string
from uuid import UUID, uuid4

from websockets.http11 import Request
from websockets.exceptions import ConnectionClosed
from websockets.asyncio.server import ServerConnection, serve

from pywssocks.common import PortPool
from pywssocks.relay import Relay
from pywssocks import __version__

_default_logger = logging.getLogger(__name__)


class SocketManager:
    """Manages server sockets with reuse capability"""

    def __init__(
        self, host: str, grace: float = 30, logger: Optional[logging.Logger] = None
    ):
        """
        Args:
            host: Listen address for servers
        """
        self._host = host
        self._grace = grace
        self._sockets: dict[int, tuple[socket.socket, float, int]] = (
            {}
        )  # port -> (socket, timestamp, refs)
        self._lock = asyncio.Lock()
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._log = logger or _default_logger

    async def get_socket(self, port: int) -> socket.socket:
        """Get a socket for the specified port, reusing existing one if available

        Args:
            port: Port number for the socket

        Returns:
            socket.socket: Socket bound to the specified port
        """
        async with self._lock:
            # Check if we have an existing socket
            if port in self._sockets:
                sock, timestamp, refs = self._sockets[port]
                self._sockets[port] = (sock, timestamp, refs + 1)
                sock.listen(1)
                self._log.debug(
                    f"Reusing existing socket for port {port} (refs: {refs + 1})"
                )
                return sock

            # Create new socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind((self._host, port))
            sock.listen(5)
            sock.setblocking(False)

            self._sockets[port] = (sock, 0, 1)
            self._log.debug(f"New socket allocated on {self._host}:{port}")
            return sock

    async def release_socket(self, port: int) -> None:
        """Release a socket, starting 30s grace period for potential reuse

        Args:
            port: Port number of the socket to release
        """
        async with self._lock:
            if port not in self._sockets:
                self._log.warning(
                    f"Attempted to release non-existent socket on port {port}"
                )
                return

            sock, _, refs = self._sockets[port]
            refs -= 1

            if refs <= 0:
                self._log.debug(f"Starting grace period for socket on port {port}")
                sock.listen(0)
                # Start grace period
                self._sockets[port] = (sock, asyncio.get_event_loop().time(), 0)
                task = asyncio.create_task(self._cleanup_socket(port))
                self._cleanup_tasks.add(task)
                task.add_done_callback(self._cleanup_tasks.discard)
            else:
                self._log.debug(f"Released socket on port {port}.")
                self._sockets[port] = (sock, 0, refs)

    async def _close_socket(self, sock: socket.socket) -> None:
        """Close a single socket safely."""

        loop = asyncio.get_running_loop()
        # Required for Python 3.8:
        #   bpo-85489: sock_accept() does not remove server socket reader on cancellation
        #         url: https://bugs.python.org/issue41317
        try:
            loop.remove_reader(sock.fileno())
        except:
            pass
        try:
            sock.close()
        except:
            pass

    async def _cleanup_socket(self, port: int) -> None:
        """Clean up socket after grace period if not reused"""

        await asyncio.sleep(self._grace)  # Grace period

        async with self._lock:
            if port not in self._sockets:
                return

            sock, timestamp, refs = self._sockets[port]
            # Only close if still in grace period (timestamp > 0) and no new refs
            if refs == 0 and timestamp > 0:
                self._log.debug(
                    f"Cleaning up unused socket on port {port} after grace period"
                )
                await self._close_socket(sock)
                del self._sockets[port]

    async def close(self) -> None:
        """Close all sockets and cancel cleanup tasks."""
        self._log.debug("Closing all managed sockets")
        async with self._lock:
            # Cancel all cleanup tasks first
            for task in self._cleanup_tasks:
                task.cancel()

            # Wait for cancellation to complete
            if self._cleanup_tasks:
                await asyncio.gather(*self._cleanup_tasks, return_exceptions=True)

            # Close all sockets
            for port, (sock, _, _) in list(self._sockets.items()):
                await self._close_socket(sock)
                del self._sockets[port]


class WSSocksServer(Relay):
    """
    A SOCKS5 over WebSocket protocol server.

    In forward proxy mode, it will receive WebSocket requests from clients, access the network as
    requested, and return the results to the client.

    In reverse proxy mode, it will receive SOCKS5 requests and send them to the connected client
    via WebSocket for parsing.
    """

    def __init__(
        self,
        ws_host: str = "0.0.0.0",
        ws_port: int = 8765,
        socks_host: str = "127.0.0.1",
        socks_port_pool: Union[PortPool, Iterable[int]] = range(1024, 10240),
        socks_wait_client: bool = True,
        socks_grace: float = 30.0,
        logger: Optional[logging.Logger] = None,
        **kw,
    ) -> None:
        """
        Args:
            ws_host: WebSocket listen address
            ws_port: WebSocket listen port
            socks_host: SOCKS5 listen address for reverse proxy
            socks_port_pool: SOCKS5 port pool for reverse proxy
            socks_wait_client: Wait for client connection before starting the SOCKS server,
                otherwise start the SOCKS server when the reverse proxy token is added.
            socks_grace: Grace time in seconds before stopping the SOCKS server after token
                removal to avoid port re-allocation.
            logger: Custom logger instance
        """

        super().__init__(logger=logger, **kw)

        self._loop = None
        self.ready = asyncio.Event()

        self._ws_host = ws_host
        self._ws_port = ws_port
        self._socks_host = socks_host

        if isinstance(socks_port_pool, PortPool):
            self._socks_port_pool = socks_port_pool
        else:
            self._socks_port_pool = PortPool(socks_port_pool)

        self._socks_wait_client = socks_wait_client

        self._pending_tokens = []

        # Store all connected reverse proxy clients, {client_id: websocket}
        self._clients: dict[UUID, ServerConnection] = {}

        # Protect shared resource for token, {token: lock}
        self._token_locks: dict[str, asyncio.Lock] = {}

        # Group reverse proxy clients by token, {token: list of (client_id, websocket) tuples}
        self._token_clients: dict[str, list[tuple[UUID, ServerConnection]]] = {}

        # Store current round-robin index for each reverse proxy token for load balancing, {token: current_index}
        self._token_indexes: dict[str, int] = {}

        # Map reverse proxy tokens to their assigned SOCKS5 ports, {token: socks_port}
        self._tokens: dict[str, int] = {}

        # Store all running SOCKS5 server tasks, {socks_port: Task}
        self._socks_tasks: dict[int, asyncio.Task] = {}

        # Message channels for receiving and routing from WebSocket, {channel_id: Queue}
        self._message_queues: dict[str, asyncio.Queue] = {}

        # Store SOCKS5 auth credentials, {token: (username, password)}
        self._socks_auth: dict[str, tuple[str, str]] = {}

        # Store tokens for forward proxy
        self._forward_tokens = set()

        # Store all connected forward proxy clients, {client_id: websocket}
        self._forward_clients: dict[UUID, ServerConnection] = {}

        # Manage SOCKS server port allocation
        self._socket_manager = SocketManager(
            socks_host, grace=socks_grace, logger=self._log
        )

    def add_reverse_token(
        self,
        token: Optional[str] = None,
        port: Optional[int] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> Union[Tuple[str, int], Tuple[None, None]]:
        """Add a new token for reverse socks and assign a port

        Args:
            token: Auth token, auto-generated if None
            port: Specific port to use, if None will allocate from port range
            username: SOCKS5 username, no auth if None
            password: SOCKS5 password, no auth if None

        Returns:
            (token, port) tuple containing the token and assigned SOCKS5 port
            Returns (None, None) if no ports available or port already in use
        """
        # If token is None, generate a random token
        if token is None:
            chars = string.ascii_letters + string.digits
            token = "".join(random.choice(chars) for _ in range(16))

        if token in self._tokens:
            return token, self._tokens[token]

        port = self._socks_port_pool.get(port)
        if port:
            self._tokens[token] = port
            self._token_locks[token] = asyncio.Lock()
            if username is not None and password is not None:
                self._socks_auth[token] = (username, password)
            if self._loop:
                self._loop.create_task(self._handle_pending_token(token))
            else:
                self._pending_tokens.append(token)
            self._log.info(f"New reverse proxy token added for port {port}.")
            return token, port
        else:
            return None, None

    def add_forward_token(self, token: Optional[str] = None) -> str:
        """Add a new token for forward socks proxy

        Args:
            token: Auth token, auto-generated if None

        Returns:
            token string
        """
        if token is None:
            chars = string.ascii_letters + string.digits
            token = "".join(random.choice(chars) for _ in range(16))

        self._forward_tokens.add(token)
        self._log.info("New forward proxy token added.")
        return token

    def remove_token(self, token: str) -> bool:
        """Remove a token and disconnect all its clients

        Args:
            token: The token to remove

        Returns:
            bool: True if token was found and removed, False otherwise
        """
        # Check if token exists
        if token not in self._tokens and token not in self._forward_tokens:
            return False

        # Handle reverse proxy token
        if token in self._tokens:
            # Get the associated port
            port = self._tokens[token]

            # Close all client connections for this token
            if token in self._token_clients:
                for client_id, ws in self._token_clients[token]:
                    if self._loop:
                        try:
                            self._loop.create_task(ws.close(1000, "Token removed"))
                        except:
                            pass
                    if client_id in self._clients:
                        del self._clients[client_id]
                del self._token_clients[token]

            # Clean up token related data
            del self._tokens[token]
            if token in self._token_locks:
                del self._token_locks[token]
            if token in self._token_indexes:
                del self._token_indexes[token]
            if token in self._socks_auth:
                del self._socks_auth[token]
            try:
                self._pending_tokens.remove(token)
            except ValueError:
                pass

            # Close and clean up SOCKS server if it exists
            if port in self._socks_tasks:
                try:
                    self._socks_tasks[port].cancel()
                except:
                    pass
                finally:
                    del self._socks_tasks[port]

            # Return port to pool
            self._socks_port_pool.put(port)

            self._log.info(f"The reverse token {token} is removed.")

        # Handle forward proxy token
        elif token in self._forward_tokens:
            # Close all forward client connections using this token
            clients_to_remove = []
            for client_id, ws in self._forward_clients.items():
                if self._loop:
                    try:
                        self._loop.create_task(ws.close(1000, "Token removed"))
                    except:
                        pass
                clients_to_remove.append(client_id)

            for client_id in clients_to_remove:
                del self._forward_clients[client_id]

            self._forward_tokens.remove(token)

            self._log.info(f"The forward token {token} is removed.")
        else:
            return False
        return True

    async def wait_ready(self, timeout: Optional[float] = None) -> asyncio.Task:
        """Start the client and connect to the server within the specified timeout, then returns the Task."""

        task = asyncio.create_task(self.serve())
        if timeout:
            await asyncio.wait_for(self.ready.wait(), timeout=timeout)
        else:
            await self.ready.wait()
        return task

    async def serve(self):
        """
        Start the server and wait clients to connect.

        This function will execute until the server is terminated.
        """

        self._loop = asyncio.get_running_loop()

        for token in self._pending_tokens:
            await self._handle_pending_token(token)
        self._pending_tokens = []

        try:
            async with serve(
                self._handle_websocket,
                self._ws_host,
                self._ws_port,
                process_request=self._process_request,
                logger=self._log.getChild("ws"),
            ):
                self._log.info(
                    f"Pywssocks Server {__version__} started on: "
                    f"ws://{self._ws_host}:{self._ws_port}"
                )
                self._log.info(f"Waiting for clients to connect.")
                self.ready.set()
                await asyncio.Future()  # Keep server running
        finally:
            await self._socket_manager.close()

    async def _get_next_websocket(self, token: str) -> Optional[ServerConnection]:
        """Get next available WebSocket connection using round-robin"""

        lock = self._token_locks[token]
        async with lock:
            if token not in self._token_clients or not self._token_clients[token]:
                return None

            clients = self._token_clients[token]
            if not clients:
                return None

            current_index = self._token_indexes.get(token, 0)
            self._token_indexes[token] = current_index = (current_index + 1) % len(
                clients
            )

        self._log.debug(
            f"Handling request using client index for this client: {current_index}"
        )
        try:
            return clients[current_index][1]
        except:
            return clients[0][1]

    async def _handle_socks_request(
        self, socks_socket: socket.socket, addr: str, token: str
    ) -> None:
        # Check if token has valid clients
        if token not in self._token_clients:
            # Wait up to 10 seconds to see if any clients connect
            loop = asyncio.get_running_loop()
            wait_start = loop.time()
            while loop.time() - wait_start < 10:
                if token in self._token_clients and self._token_clients[token]:
                    break
                await asyncio.sleep(0.1)
            else:
                self._log.debug(
                    f"No valid clients for token after waiting 10s, refusing connection from {addr}"
                )
                return await self._refuse_socks_request(socks_socket, 3)

        # Use round-robin to get websocket connection
        websocket = await self._get_next_websocket(token)
        if not websocket:
            self._log.warning(
                f"No available client for SOCKS5 port {self._tokens[token]}."
            )
            return await self._refuse_socks_request(socks_socket, 3)
        auth = self._socks_auth.get(token, None)
        if auth:
            socks_username, socks_password = auth
        else:
            socks_username = socks_password = None
        return await super()._handle_socks_request(
            websocket, socks_socket, socks_username, socks_password
        )

    async def _handle_pending_token(
        self, token: str, ready_event: Optional[asyncio.Event] = None
    ):
        if not self._socks_wait_client:
            socks_port = self._tokens.get(token, None)
            lock = self._token_locks[token]
            async with lock:
                if socks_port and (socks_port not in self._socks_tasks):
                    self._socks_tasks[socks_port] = task = asyncio.create_task(
                        self._run_socks_server(
                            token, socks_port, ready_event=ready_event
                        )
                    )
                    return task

    async def _handle_websocket(self, websocket: ServerConnection) -> None:
        """Handle WebSocket connection"""
        client_id = None
        token = None
        socks_port = None

        try:
            # Wait for authentication message
            auth_message = await websocket.recv()
            auth_data = json.loads(auth_message)

            if auth_data.get("type", None) != "auth":
                await websocket.close(1008, "Invalid auth message")
                return

            token = auth_data.get("token", None)
            reverse = auth_data.get("reverse", None)

            # Validate token and generate client_id only after successful authentication
            if reverse == True and token in self._tokens:  # reverse proxy
                client_id = uuid4()  # Generate UUID after successful auth
                socks_port = self._tokens[token]
                lock = self._token_locks[token]

                async with lock:
                    if token not in self._token_clients:
                        self._token_clients[token] = []
                    self._token_clients[token].append((client_id, websocket))

                    # Ensure SOCKS server is running
                    if socks_port not in self._socks_tasks:
                        self._socks_tasks[socks_port] = asyncio.create_task(
                            self._run_socks_server(token, socks_port)
                        )

                self._clients[client_id] = websocket
                await websocket.send(
                    json.dumps({"type": "auth_response", "success": True})
                )
                self._log.info(f"Reverse client {client_id} authenticated")

            elif reverse == False and token in self._forward_tokens:  # forward proxy
                client_id = uuid4()  # Generate UUID after successful auth
                self._forward_clients[client_id] = websocket
                await websocket.send(
                    json.dumps({"type": "auth_response", "success": True})
                )
                self._log.info(f"Forward client {client_id} authenticated")

            else:
                await websocket.send(
                    json.dumps({"type": "auth_response", "success": False})
                )
                await websocket.close(1008, "Invalid token")
                return

            # Only proceed with message handling if authentication was successful
            receiver_task = asyncio.create_task(
                self._message_dispatcher(websocket, client_id)
            )
            heartbeat_task = asyncio.create_task(
                self._ws_heartbeat(websocket, client_id)
            )

            tasks = [receiver_task, heartbeat_task]
            try:
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    try:
                        task.result()
                    except Exception as e:
                        if not isinstance(e, asyncio.CancelledError):
                            self._log.error(
                                f"Task failed with error: {e.__class__.__name__}: {e}."
                            )
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            self._log.error(f"WebSocket processing error: {e.__class__.__name__}: {e}.")
        finally:
            if client_id:
                self._log.info(f"Client {client_id} disconnected.")
            else:
                self._log.info(f"Client (unauthenticated) disconnected.")
            await self._cleanup_connection(client_id, token)

    async def _cleanup_connection(
        self, client_id: Optional[UUID], token: Optional[str]
    ) -> None:
        """Clean up resources without closing SOCKS server"""

        if not client_id or not token:
            return

        # Clean up _token_clients
        if token in self._token_clients:
            self._token_clients[token] = [
                (cid, ws) for cid, ws in self._token_clients[token] if cid != client_id
            ]

            # Clean up resources if no connections left for this token
            if not self._token_clients[token]:
                del self._token_clients[token]
                if token in self._token_indexes:
                    del self._token_indexes[token]

        # Clean up _clients
        if client_id in self._clients:
            del self._clients[client_id]

        self._log.debug(f"Cleaned up resources for client {client_id}.")

    async def _ws_heartbeat(self, websocket: ServerConnection, client_id: UUID) -> None:
        """WebSocket heartbeat check"""
        try:
            while True:
                try:
                    # Send ping every 30 seconds
                    await websocket.ping()
                    await asyncio.sleep(30)
                except ConnectionClosed:
                    self._log.info(
                        f"Heartbeat detected disconnection for client {client_id}."
                    )
                    break
                except Exception as e:
                    self._log.error(f"Heartbeat error for client {client_id}: {e}")
                    break
        finally:
            # Ensure WebSocket is closed
            try:
                await websocket.close()
            except:
                pass

    async def _message_dispatcher(
        self, websocket: ServerConnection, client_id: UUID
    ) -> None:
        """WebSocket message receiver distributing messages to different message queues"""

        network_handler_tasks = set()  # Track network connection handler tasks

        try:
            while True:
                try:
                    msg = await asyncio.wait_for(
                        websocket.recv(), timeout=60
                    )  # 60 seconds timeout
                    data = json.loads(msg)

                    if data["type"] == "data":
                        channel_id = data["channel_id"]
                        self._log.debug(f"Received data for channel: {channel_id}")
                        if channel_id in self._message_queues:
                            await self._message_queues[channel_id].put(data)
                        else:
                            self._log.debug(
                                f"Received data for unknown channel: {channel_id}"
                            )
                    elif data["type"] == "connect_response":
                        self._log.debug(f"Received network connection response: {data}")
                        connect_id = data["connect_id"]
                        if connect_id in self._message_queues:
                            await self._message_queues[connect_id].put(data)
                    elif (
                        data["type"] == "connect" and client_id in self._forward_clients
                    ):
                        self._log.debug(f"Received network connection request: {data}")
                        handler_task = asyncio.create_task(
                            self._handle_network_connection(websocket, data)
                        )
                        network_handler_tasks.add(handler_task)
                        handler_task.add_done_callback(network_handler_tasks.discard)
                except asyncio.TimeoutError:
                    # If 60 seconds pass without receiving messages, check if connection is still alive
                    try:
                        await websocket.ping()
                    except:
                        self._log.warning(f"Connection timeout for client {client_id}")
                        break
                except ConnectionClosed:
                    self._log.info(f"Client {client_id} connection closed.")
                    break
        except Exception as e:
            self._log.error(
                f"WebSocket receive error for client {client_id}: {e.__class__.__name__}: {e}."
            )
        finally:
            # Cancel all active network connection handler tasks
            for task in network_handler_tasks:
                task.cancel()
            await asyncio.gather(*network_handler_tasks, return_exceptions=True)

    async def _run_socks_server(
        self, token: str, socks_port: int, ready_event: Optional[asyncio.Event] = None
    ) -> None:
        """SOCKS server startup function"""

        socks_handler_tasks = set()  # Track SOCKS request handler tasks

        try:
            socks_server = await self._socket_manager.get_socket(socks_port)
            self._log.info(
                f"SOCKS5 server socket allocated on {self._socks_host}:{socks_port}"
            )

            loop = asyncio.get_event_loop()
            if ready_event:
                ready_event.set()
            while True:
                try:
                    client_sock, addr = await loop.sock_accept(socks_server)
                    self._log.debug(f"Accepted SOCKS5 connection from {addr}.")
                    handler_task = asyncio.create_task(
                        self._handle_socks_request(client_sock, addr, token)
                    )
                    socks_handler_tasks.add(handler_task)
                    handler_task.add_done_callback(socks_handler_tasks.discard)
                except Exception as e:
                    self._log.error(
                        f"Error accepting SOCKS connection: {e.__class__.__name__}: {e}"
                    )
        except Exception as e:
            self._log.error(f"SOCKS server error: {e}")
        except asyncio.CancelledError:
            pass
        finally:
            # Cancel all active SOCKS request handler tasks
            for task in socks_handler_tasks:
                task.cancel()
            await asyncio.gather(*socks_handler_tasks, return_exceptions=True)

            # Release the socket (starts grace period)
            await self._socket_manager.release_socket(socks_port)
            self._log.info(
                f"SOCKS5 server released on {self._socks_host}:{socks_port}."
            )

    async def _process_request(self, connection: ServerConnection, request: Request):
        """Process HTTP requests before WebSocket handshake"""

        if request.path == "/socket":
            # Return None to continue WebSocket handshake for WebSocket path
            return None
        elif request.path == "/":
            respond = (
                f"Pywssocks {__version__} is running but API is not enabled. "
                "Please check the documentation.\n"
            )
            return connection.respond(HTTPStatus.OK, respond)
        else:
            return connection.respond(HTTPStatus.NOT_FOUND, "404 Not Found\n")
