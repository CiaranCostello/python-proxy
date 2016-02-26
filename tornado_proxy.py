import sys, socket, logging, tornado.httpclient, tornado.web, tornado.ioloop, redis, tornado.template, pickle
from urllib.parse import urlparse

blocked_urls = ['www.cbrenn.xyz','www.youtube.com','imgur.com']

# Provides a management console also on port 8888. 
# Shows a list of blocked urls and gives the option to add to or remove from the list.
class ManagementConsole(tornado.web.RequestHandler):
	SUPPORTED_METHODS = ['GET', 'POST']

	@tornado.web.asynchronous
	def get(self):
		self.render('management_console.html', list=blocked_urls)

	@tornado.web.asynchronous
	def post(self):
		blocked_urls.append(self.get_argument('block_url', None))
		try:
			blocked_urls.remove(self.get_argument('unblock_url', ''))
		except ValueError:
			pass
		self.render('management_console.html', list=blocked_urls)

class MainHandler(tornado.web.RequestHandler):
	SUPPORTED_METHODS = ['GET', 'POST', 'CONNECT']

	#disable etags as not handled by server
	def compute_etag(self):
		return None

	@tornado.web.asynchronous
	def get(self):
		#output the type of request and the url it is accessing
		print('Handling {0} request to {1}'.format(self.request.method, self.request.uri))
		#sends the response from the server to the client
		def response_handler(response):
			if (response.error and not isinstance(response.error, tornado.httpclient.HTTPError)):
				self.set_status(500)
				self.write('Internal server error:\n' + str(response.error))
			else:
				#handle caching
				url = response.request.url
				#if it already exists in the cache then no need to cache again
				if not cache.exists(url):
					print('Storing result of request to {0} in cache'.format(url))
					bytestream = pickle.dumps(response)
					cache.set(url, bytestream)
					#set max age of the cache entry
					cache.expire(url, cache_control_time(response.headers.get_list('Cache-Control')))
				#handle headers
				self.set_status(response.code, response.reason)
				self._headers = tornado.httputil.HTTPHeaders()
				for h, v in response.headers.get_all():
					if h not in ('Content-Length', 'Transfer-Encoding', 'Content-Encoding', 'Connection'):
						self.add_header(h, v)
				if response.body:
					self.write(response.body)
			self.finish()
		#Check if the url is being blocked
		base_url = urlparse(self.request.uri).hostname
		if base_url in blocked_urls:
			print('Get {0} blocked'.format(base_url))
			self.write('This url is blocked by the proxy')
			self.finish()
		#otherwise
		else:
		#sends request to the server and uses callback to call response handler
			fetch_coroutine(
					self.request.uri, response_handler, 
					method=self.request.method, body=self.request.body,
					headers=self.request.headers, follow_redirects=False,
					allow_nonstandard_methods=True)

			
	@tornado.web.asynchronous
	def post(self):
		return self.get()

	# handle https connections
	# provide tunnelling from client to server
	@tornado.web.asynchronous
	def connect(self):
		print('Start CONNECT to {0}'.format(self.request.uri))
		base_url = urlparse(self.request.uri)
		base_url = self.request.uri.split(':')[0]
		# block any blocked urls
		if base_url in blocked_urls:
			print('CONNECT {0} blocked'.format(base_url))
			self.write('This url is blocked by the proxy')
			self.finish()
		# other wise establish a connection
		else:
			host, port = self.request.uri.split(':')
			#get client socket
			client = self.request.connection.stream

			def forward_from_client(data):
				try:
					server.write(data)
				except tornado.iostream.StreamClosedError:
					close_client()
					close_server()

			def forward_from_server(data):
				try:
					client.write(data)
				except tornado.iostream.StreamClosedError:
					close_client()
					close_server()

			def close_client(data=None):
				if client.closed():
					return
				if data:
					client.write(data)
				client.close()

			def close_server(data=None):
				if server.closed():
					return
				if data:
					server.write()
				server.close()

			def start_tunnel():
				print('Tunnel started to {0}'.format(self.request.uri))
				# forward all traffic in either direction
				client.read_until_close(close_server, forward_from_client)
				server.read_until_close(close_client, forward_from_server)
				# enform client connection is open to start traffic
				client.write(b'HTTP/1.0 200 Connection established\r\n\r\n')

			# open socket and connect to server using host and port
			s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
			server = tornado.iostream.IOStream(s)
			# also start tunnel on connection
			server.connect((host, int(port)), start_tunnel)

# called by MainHandler.get()
# forms request. Gets response from handler and calls callback
def fetch_coroutine(url, callback, **kwargs):
	req = tornado.httpclient.HTTPRequest(url, **kwargs)
	client = tornado.httpclient.AsyncHTTPClient()
	#add caching here of the request 
	pickled_response = cache.get(url)
	#if url is not in the cache
	if pickled_response == None:
		response = client.fetch(req, callback, raise_error=False)
	#if it is in the cache
	else:
		response = pickle.loads(pickled_response)
		print('								Got request to {0} from cache'.format(url))
		callback(response)


def cache_control_time(control):
	for instruction in control:
		if 'max-age=' in instruction:
			print(instruction)
			parts = instruction.split('=')
			parts = parts[1].split(',')[0]
			print('Hold in cache for {0} seconds'.format(parts))
			return int(parts)
	return 0


def make_app():
	return tornado.web.Application([
		(r"/admin", ManagementConsole), (r".*", MainHandler),
	])

def make_cache(maxmemory=0, policy='allkeys-lru'):
	s = redis.StrictRedis(host='localhost', port=6379, db=0)
	#set memory available to use as a cache. For no memory limits set to 0
	s.config_set('maxmemory', maxmemory)
	#set replacement policy
	s.config_set('maxmemory-policy', policy)
	s.flushall()
	return s


#global cache object
cache = make_cache()

if __name__ == "__main__":
	app = make_app()
	app.listen(8888)
	try:
		tornado.ioloop.IOLoop.current().start()
	except KeyboardInterrupt:
		print("Ctrl C - Stopping server")
		sys.exit(1)