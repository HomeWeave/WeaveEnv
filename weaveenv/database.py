from peewee import SqliteDatabase, Proxy, Model, CharField, BooleanField
from peewee import ForeignKeyField


proxy = Proxy()


class BaseModel(Model):
    class Meta(object):
        database = proxy


class WeaveEnvInstanceData(BaseModel):
    machine_id = CharField(unique=True)
    app_token = CharField()


class PluginData(BaseModel):
    app_id = CharField(unique=True)
    enabled = BooleanField(default=False)
    is_remote = BooleanField(default=False)
    machine = ForeignKeyField(WeaveEnvInstanceData, backref='plugins')


class PluginsDatabase(object):
    def __init__(self, path):
        self.conn = SqliteDatabase(path)

    def start(self):
        proxy.initialize(self.conn)
        self.conn.create_tables([
            PluginData,
            WeaveEnvInstanceData,
        ])
