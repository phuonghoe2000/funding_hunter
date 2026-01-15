import gzip

# ... (imports)

    async def send_json(self, payload: Dict):
        """Send JSON payload to WS"""
        if self._connected and self.ws:
            try:
                await self.ws.send_json(payload)
            except Exception as e:
                logger.error(f"WS send error: {e}")

    # ... (existing subscribe/unsubscribe use send_json internally or directly)

    async def _monitor_connection(self):
        """Main loop ensuring connection stays alive"""
        while self._running:
            try:
                if not self.session:
                    self.session = aiohttp.ClientSession()
                    
                logger.info(f"Connecting to WS: {self.url}...")
                async with self.session.ws_connect(self.url, heartbeat=self.ping_interval) as ws:
                    self.ws = ws
                    self._connected = True
                    self.last_msg_time = time.time()
                    logger.info("WS Connected")
                    
                    # Resubscribe to existing topics
                    for sub in self._subscriptions:
                        await ws.send_json(sub)
                        
                    async for msg in ws:
                        if not self._running:
                            break
                        
                        self.last_msg_time = time.time()
                        
                        data = None
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                            except Exception as e:
                                logger.error(f"WS JSON error: {e}")
                                
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            try:
                                # Decompress GZIP
                                decompressed = gzip.decompress(msg.data)
                                data = json.loads(decompressed)
                            except Exception as e:
                                logger.error(f"WS GZIP/JSON error: {e}")
                        
                        if data:
                             for cb in self._callbacks:
                                 cb(data)

                        if msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error(f"WS error: {ws.exception()}")
                            break
                            
                    self._connected = False
                    logger.warning("WS Disconnected")
                    
            except Exception as e:
                self._connected = False
                logger.error(f"WS connection failed: {e}")
                
            if self._running:
                logger.info(f"Reconnecting in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
