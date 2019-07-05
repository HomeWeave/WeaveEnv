from peewee import SqliteDatabase, Proxy, Model, CharField, BooleanField
from peewee import ForeignKeyField, CompositeKey


proxy = Proxy()


class BaseModel(Model):
    class Meta(object):
        database = proxy


class WeaveEnvInstanceData(BaseModel):
    machine_id = CharField(primary_key=True)
    app_token = CharField()


class PluginData(BaseModel):
    app_url = CharField()
    name = CharField()
    description = CharField(default="")
    enabled = BooleanField(default=False)
    machine = ForeignKeyField(WeaveEnvInstanceData, backref='plugins')

    class Meta:
        primary_key = CompositeKey('app_url', 'machine')


class PluginsDatabase(object):
    def __init__(self, path):
        self.conn = SqliteDatabase(path)

    def start(self):
        proxy.initialize(self.conn)
        self.conn.create_tables([
            PluginData,
            WeaveEnvInstanceData,
        ])
