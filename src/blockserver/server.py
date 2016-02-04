import logging

import tornado
import tornado.httpserver
from tornado import concurrent
from tornado import gen
from tornado.httpclient import AsyncHTTPClient
from tornado.options import define, options
from tornado.web import Application, RequestHandler, stream_request_body
import tempfile
from typing import Callable
from blockserver.backends.util import StorageObject, Transfer

define('debug', help="Enable debug output for tornado", default=False)
define('asyncio', help="Run on the asyncio loop instead of the tornado IOLoop", default=False)
define('dummy', help="Dummy storage backend instead of s3 backend", default=False)
define('transfers', help="Thread pool size for transfers", default=10)
define('port', default='8888')
define('apisecret', help="API_SECRET of the accounting server", default='secret')
define('noauth', help="Disable authentication", default=False)
define('dummy_auth', help="Authenticate with the magicauth-token", default=False)
define('magicauth', default="Token MAGICFARYDUST")
define('accountingserver', help="Base url to the accounting server", default="http://localhost:8000")
define('dummylog', help="Instead of calling the accounting server for logging, log to stdout",
       default=False)
logger = logging.getLogger(__name__)

async def check_auth(auth, prefix, file_path, action):
    if options.noauth:
        return True
    if options.dummy_auth:
        return await dummy_auth(auth, prefix, file_path, action)
    http_client = AsyncHTTPClient()
    url = options.accountingserver + '/api/v0/auth/' + prefix + '/' + file_path
    response = await http_client.fetch(
        url, method=action, headers={'Authorization': auth},
        body=b'' if action == 'POST' else None, raise_error=False,
    )
    return response.code == 204


async def dummy_auth(auth, prefix, file_path, action):
    return auth == options.magicauth and prefix == 'test'


@stream_request_body
class FileHandler(RequestHandler):
    auth = None
    streamer = None

    def initialize(self, transfer_cls: Callable[[], Callable[[], Transfer]],
                   auth_callback: Callable[[], Callable[[str, str, str, str], bool]],
                   log_callback: Callable[[], Callable[[str, str, str, int], None]]):
        self._thread_pool = concurrent.futures.ThreadPoolExecutor(options.transfers)
        self.transfer = transfer_cls()()
        self.auth_callback = auth_callback()
        self.log_callback = log_callback()

    async def prepare(self):
        self.auth = None
        self.streamer = None
        try:
            prefix = self.path_kwargs['prefix']
            file_path = self.path_kwargs['file_path']
        except KeyError:
            self.send_error(403)
            return
        self.auth_header = self.request.headers.get('Authorization', None)
        if not await self.auth_callback(self.auth_header, prefix, file_path, self.request.method):
            self.auth = False
            self.send_error(403, reason="Not authorized for this prefix")
            return
        else:
            self.auth = True
        if self.request.method == 'POST':
            self.temp = tempfile.NamedTemporaryFile(delete=False)

    async def data_received(self, chunk):
        if not self.auth:
            self.send_error()
            return
        self.temp.write(chunk)

    @gen.coroutine
    def get(self, prefix, file_path):
        etag = self.request.headers.get('If-None-Match', None)
        storage_object = yield self.retrieve_file(prefix, file_path, etag)
        if storage_object is None:
            self.send_error(404)
            return
        self.set_header('ETag', storage_object.etag)
        if storage_object.local_file is None:
            self.set_status(304)
        else:
            with open(storage_object.local_file, 'rb') as f_in:
                for chunk in iter(lambda: f_in.read(8192), b''):
                    self.write(chunk)
        self.finish()

    @gen.coroutine
    def post(self, prefix, file_path):
        self.temp.close()
        storage_object = yield self.store_file(prefix, file_path, self.temp.name)
        self.set_status(204)
        self.set_header('ETag', storage_object.etag)
        self.finish()

    @gen.coroutine
    def delete(self, prefix, file_path):
        yield self.delete_file(prefix, file_path)
        self.set_status(204)
        self.finish()

    @concurrent.run_on_executor(executor='_thread_pool')
    def delete_file(self, prefix, file_path):
        return self.transfer.delete(StorageObject(prefix, file_path, None, None))

    @concurrent.run_on_executor(executor='_thread_pool')
    def store_file(self, prefix, file_path, filename):
        return self.transfer.store(StorageObject(prefix, file_path, None, filename))

    @concurrent.run_on_executor(executor='_thread_pool')
    def retrieve_file(self, prefix, file_path, etag):
        return self.transfer.retrieve(StorageObject(prefix, file_path, etag, None))


def main():
    tornado.options.parse_command_line()
    if options.dummy:
        from blockserver.backends.dummy import Transfer
    else:
        from blockserver.backends.s3 import Transfer
    application = make_app(
        transfer_cls=lambda: Transfer,
        auth_callback=lambda: dummy_auth if options.dummy else check_auth,
        log_callback=lambda: None, debug=options.debug
    )
    if options.debug:
        application.listen(options.port)
    else:
        server = tornado.httpserver.HTTPServer(application)
        server.bind(options.port)
        server.start(0)
    if options.asyncio:
        logger.info('Using asyncio')
        from tornado.platform.asyncio import AsyncIOMainLoop
        AsyncIOMainLoop.current().start()
    else:
        logger.info('Using IOLoop')
        from tornado.ioloop import IOLoop
        IOLoop.current().start()


def make_app(transfer_cls, auth_callback, log_callback, debug):
    application = Application([
        (r'^/api/v0/files/(?P<prefix>[\d\w-]+)/(?P<file_path>[\d\w-]+)', FileHandler, dict(
            transfer_cls=transfer_cls,
            auth_callback=auth_callback,
            log_callback=log_callback,
        ))
    ], debug=debug)
    return application