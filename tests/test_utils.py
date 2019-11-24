from uuid import uuid4

from messaging.service import CoreService

from weavelib.services import BackgroundThreadServiceStart, MessagingEnabled


class MessagingService(BackgroundThreadServiceStart, CoreService):
    def __init__(self):
        self.test_token = str(uuid4())
        super(MessagingService, self).__init__(auth_token=self.test_token,
                                               started_token="test")


class DummyEnvService(MessagingEnabled):
    def __init__(self, auth_token, conn):
        super(DummyEnvService, self).__init__(auth_token=auth_token, conn=conn)

    def start(self):
        self.get_connection().connect()
