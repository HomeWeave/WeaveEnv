from peewee import SqliteDatabase, Proxy, Model, CharField, BooleanField
from peewee import DoesNotExist


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


class PluginsDatabase(object):
    def __init__(self, path):
        self.conn = SqliteDatabase(path)

    def start(self):
        proxy.initialize(self.conn)
        self.conn.create_tables([
            PluginData,
            WeaveEnvInstance,
        ])

    def query(self, key):
        try:
            return PluginData.get(PluginData.app_id == key)
        except DoesNotExist:
            raise ValueError(key)

    def insert(self, **kwargs):
        query = PluginData.insert(**kwargs)
        query.on_conflict_replace().execute()
