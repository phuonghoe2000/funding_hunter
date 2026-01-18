"""
WebSocket Manager for Exchange Connections
Handles connection, reconnection, and message routing
"""

import asyncio
import gzip
import json
import logging
import time
from typing import Callable, Dict, List, Optional, Any

import aiohttp

logger = logging.getLogger(__name__)


class WebSocketManager:
    """Manages WebSocket connection with auto-reconnect and message handling"""
    
    def __init__(
        self, 
        url: str,
        ping_interval: float = 20.0,
        reconnect_delay: float = 5.0
    ):
        """
        Initialize WebSocket Manager
        
        Args:
            url: WebSocket URL to connect to
            ping_interval: Seconds between ping messages (heartbeat)
            reconnect_delay: Seconds to wait before reconnecting
        """
        self.url = url
        self.ping_interval = ping_interval
        self._reconnect_delay = reconnect_delay
        
        # Connection state
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self._connected = False
        self._running = False
        self.last_msg_time: float = 0
        
        # Subscriptions and callbacks
        self._subscriptions: List[Dict] = []
        self._callbacks: List[Callable[[Dict], None]] = []
        
        # Task handle
        self._monitor_task: Optional[asyncio.Task] = None
    
    @property
    def connected(self) -> bool:
        """Check if WebSocket is connected"""
        return self._connected
    
    def add_callback(self, callback: Callable[[Dict], None]):
        """Add a callback to receive messages"""
        if callback not in self._callbacks:
            self._callbacks.append(callback)
    
    def remove_callback(self, callback: Callable[[Dict], None]):
        """Remove a callback"""
        if callback in self._callbacks:
            self._callbacks.remove(callback)
    
    async def connect(self):
        """Start WebSocket connection"""
        if self._running:
            return
        
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_connection())
        logger.info(f"WebSocket manager started for {self.url}")
    
    async def disconnect(self):
        """Stop WebSocket connection"""
        self._running = False
        
        if self.ws and not self.ws.closed:
            await self.ws.close()
        
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        
        if self.session:
            await self.session.close()
            self.session = None
        
        self._connected = False
        logger.info("WebSocket manager stopped")
    
    async def subscribe(self, subscription: Dict):
        """Subscribe to a topic/channel"""
        if subscription not in self._subscriptions:
            self._subscriptions.append(subscription)
        
        if self._connected and self.ws:
            try:
                await self.ws.send_json(subscription)
                logger.debug(f"Subscribed: {subscription}")
            except Exception as e:
                logger.error(f"Subscribe error: {e}")
    
    async def unsubscribe(self, subscription: Dict):
        """Unsubscribe from a topic/channel"""
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)
        
        # Note: Most exchanges require a specific unsubscribe message
        # This basic implementation just removes from local tracking
    
    async def send_json(self, payload: Dict):
        """Send JSON payload to WebSocket"""
        if self._connected and self.ws:
            try:
                await self.ws.send_json(payload)
            except Exception as e:
                logger.error(f"WS send error: {e}")
    
    async def send_str(self, message: str):
        """Send string message to WebSocket"""
        if self._connected and self.ws:
            try:
                await self.ws.send_str(message)
            except Exception as e:
                logger.error(f"WS send error: {e}")
    
    async def _monitor_connection(self):
        """Main loop ensuring connection stays alive with auto-reconnect"""
        while self._running:
            try:
                if not self.session:
                    self.session = aiohttp.ClientSession()
                
                logger.info(f"Connecting to WS: {self.url}...")
                
                async with self.session.ws_connect(
                    self.url, 
                    heartbeat=self.ping_interval,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as ws:
                    self.ws = ws
                    self._connected = True
                    self.last_msg_time = time.time()
                    logger.info("WS Connected")
                    
                    # Resubscribe to existing topics after reconnect
                    for sub in self._subscriptions:
                        try:
                            await ws.send_json(sub)
                            logger.debug(f"Resubscribed: {sub}")
                        except Exception as e:
                            logger.error(f"Resubscribe error: {e}")
                    
                    # Message loop
                    async for msg in ws:
                        if not self._running:
                            break
                        
                        self.last_msg_time = time.time()
                        
                        data = None
                        
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                            except json.JSONDecodeError as e:
                                logger.error(f"WS JSON parse error: {e}")
                        
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            try:
                                # Try GZIP decompression (used by some exchanges like BingX)
                                decompressed = gzip.decompress(msg.data)
                                data = json.loads(decompressed)
                            except gzip.BadGzipFile:
                                # Not GZIP, try raw JSON
                                try:
                                    data = json.loads(msg.data)
                                except json.JSONDecodeError as e:
                                    logger.error(f"WS binary parse error: {e}")
                            except Exception as e:
                                logger.error(f"WS GZIP/JSON error: {e}")
                        
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error(f"WS error: {ws.exception()}")
                            break
                        
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            logger.warning("WS connection closed by server")
                            break
                        
                        # Dispatch message to callbacks
                        if data:
                            for cb in self._callbacks:
                                try:
                                    cb(data)
                                except Exception as e:
                                    logger.error(f"Callback error: {e}")
                    
                    self._connected = False
                    logger.warning("WS Disconnected")
                    
            except aiohttp.ClientError as e:
                self._connected = False
                logger.error(f"WS client error: {e}")
            except asyncio.TimeoutError:
                self._connected = False
                logger.error("WS connection timeout")
            except Exception as e:
                self._connected = False
                logger.error(f"WS connection failed: {e}")
            
            # Reconnect after delay
            if self._running:
                logger.info(f"Reconnecting in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
    
    async def wait_connected(self, timeout: float = 10.0) -> bool:
        """Wait for WebSocket to connect"""
        start = time.time()
        while not self._connected and (time.time() - start) < timeout:
            await asyncio.sleep(0.1)
        return self._connected
