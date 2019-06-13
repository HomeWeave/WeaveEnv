import eventlet
eventlet.monkey_patch()  # NOLINT

import errno
import json
import logging.config
import os
import signal
import subprocess
import sys
from uuid import uuid4

import appdirs
from peewee import DoesNotExist

from weavelib.exceptions import ObjectNotFound, WeaveException
from weavelib.messaging import WeaveConnection
from weavelib.rpc import RPCClient, find_rpc
from weavelib.services.service_base import MessagingEnabled

from weaveenv.database import PluginsDatabase, WeaveEnvInstanceData, PluginData
from weaveenv.http import WeaveHTTPServer
from weaveenv.instances import get_plugin_by_url, LocalWeaveInstance
from weaveenv.plugins import PluginManager, VirtualEnvManager, GitPlugin
from weaveenv.plugins import url_to_plugin_id, load_plugin_json
from weaveenv.logging import LOGGING


logging.config.dictConfig(LOGGING)

MESSAGING_PLUGIN_URL = "https://github.com/HomeWeave/WeaveServer.git"


def get_config_path():
    if os.environ.get("WEAVE_DIR"):
        return os.environ["WEAVE_DIR"]

    weave_base = appdirs.user_data_dir("homeweave")
    try:
        os.makedirs(weave_base)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
    return weave_base


def get_machine_id():
    if sys.platform == 'darwin':
        command = ['system_profiler', 'SPHardwareDataType']
        process = subprocess.Popen(command, stdout=subprocess.PIPE)
        out, err = process.communicate()
        lines = [x.strip().decode('UTF-8') for x in out.splitlines()]
        hardware_line = next(x for x in lines if 'Hardware UUID' in x)
        return hardware_line.split()[1].strip()
    elif sys.platform == 'linux':
       with open("/sys/class/dmi/id/modalias") as inp:
          return inp.read()
    else:
       raise Exception('Unknown System')


def get_instance_data():
    try:
        return WeaveEnvInstanceData.get(WeaveEnvInstanceData.machine_id
                                            == get_machine_id())
    except DoesNotExist:
        sys.exit("Please re-install messaging plugin.")


def handle_main():
    base_path = get_config_path()
    plugins_db = PluginsDatabase(os.path.join(base_path, "db"))
    plugin_manager = PluginManager(base_path)

    plugins_db.start()

    weave = LocalWeaveInstance(get_instance_data(), plugin_manager)
    try:
        weave.start()
    except WeaveException:
        weave.stop()

    signal.signal(signal.SIGTERM, lambda x, y: weave.stop())
    signal.signal(signal.SIGINT, lambda x, y: weave.stop())

    weave.wait()


def handle_messaging_plugin_install():
    base_path = get_config_path()
    plugins_db = PluginsDatabase(os.path.join(base_path, "db"))
    plugin_manager = PluginManager(base_path)

    plugins_db.start()

    git_plugin = GitPlugin(MESSAGING_PLUGIN_URL, "WeaveServer", "Messaging")
    plugin_manager.install(git_plugin)

    token = "app-token-" + str(uuid4())
    instance_data = WeaveEnvInstanceData(machine_id=get_machine_id(),
                                         app_token=token)
    plugin_data = PluginData(app_id=url_to_plugin_id(MESSAGING_PLUGIN_URL),
                             name="WeaveServer", description="Messaging",
                             enabled=True, machine=instance_data)
    plugin_data.save(force_insert=True)
    instance_data.save(force_insert=True)


def handle_rpc():
    class FakeService(MessagingEnabled):
        def __init__(self, auth_token, conn):
            super(FakeService, self).__init__(auth_token=auth_token,
                                                        conn=conn)

        def start(self):
            self.get_connection().connect()

    app_url = sys.argv[1]
    rpc_name = sys.argv[2]
    api_name = sys.argv[3]
    json_args = sys.argv[4]

    plugins_db = PluginsDatabase(os.path.join(get_config_path(), "db"))
    plugins_db.start()

    conn = WeaveConnection.discover()
    conn.connect()

    instance_data = get_instance_data()
    token = instance_data.app_token

    rpc_info = find_rpc(FakeService(token, conn), app_url, rpc_name)
    client = RPCClient(conn, rpc_info, token)
    client.start()

    print(client[api_name](*json.loads(json_args), _block=True))



def handle_weave_launch():
    params = json.loads(sys.stdin.readline().strip())

    plugin_dir = params["plugin_dir"]
    os.chdir(plugin_dir)
    sys.path.append(plugin_dir)

    if params.get("venv_dir"):
        venv = VirtualEnvManager(params["venv_dir"])
        venv.activate()

    plugin_info = load_plugin_json(plugin_dir)

    if issubclass(plugin_info["service_cls"], MessagingEnabled):
        conn = WeaveConnection.discover()
        conn.connect()
        params["conn"] = conn

    app = plugin_info["service_cls"](**params)

    signal.signal(signal.SIGTERM, lambda x, y: app.on_service_stop())
    signal.signal(signal.SIGINT, lambda x, y: app.on_service_stop())

    app.before_service_start()
    app.on_service_start()


def handle_discover():
    conn = WeaveConnection.discover()
    print(conn.default_host, conn.default_port)
