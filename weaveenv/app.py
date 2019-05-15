import eventlet
eventlet.monkey_patch()  # NOLINT

import errno
import logging
import os
import signal
import sys
from uuid import uuid4

import appdirs

from weavelib.exceptions import ObjectNotFound
from weavelib.messaging import WeaveConnection
from weavelib.services.service_base import MessagingEnabled

from weaveenv.database import PluginsDatabase, WeaveEnvInstanceData, PluginData
from weaveenv.http import WeaveHTTPServer
from weaveenv.instances import get_plugin_by_url
from weaveenv.plugins import PluginManager, VirtualEnvManager, GitPlugin
from weaveenv.plugins import url_to_plugin_id


logging.basicConfig()


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
    return os.path.join(weave_base, "weaveenv.db")


def get_machine_id():
    with open("/sys/class/dmi/id/modalias") as inp:
        return inp.read()


def handle_main():
    machine_id = get_machine_id()
    base_path = get_config_path()
    plugins_db = PluginsDatabase(os.path.join(base_path, "db"))
    plugin_manager = PluginManager(base_path)

    plugins_db.start()

    # Check if the messaging plugin is installed any machine.
    try:
        messaging_plugin = get_plugin_by_url(MESSAGING_PLUGIN_URL)
    except ObjectNotFound:
        print("No messaging plugin installed.")
        sys.exit(1)

    if messaging_plugin.machine.machine_id != machine_id:
        conn = WeaveConnection.discover()
    else:
        conn = WeaveConnection.local()
    conn.connect()

    auth_token = messaging_plugin.machine.app_token

    service = MessagingEnabled(auth_token=auth_token, conn=conn)
    instance_data = \
        WeaveEnvInstanceData.get(WeaveEnvInstanceData.machine_id == machine_id)

    weave = LocalWeaveInstance(service, instance_data, plugin_manager)
    weave.start()

    signal.signal(signal.SIGTERM, lambda x, y: weave.stop())
    signal.signal(signal.SIGINT, lambda x, y: weave.stop())

    weave.wait()


def handle_messaging_plugin_install():
    machine_id = get_machine_id()
    base_path = get_config_path()
    plugins_db = PluginsDatabase(os.path.join(base_path, "db"))
    plugin_manager = PluginManager(base_path)

    plugins_db.start()

    git_plugin = GitPlugin(MESSAGING_PLUGIN_URL, "WeaveServer", "Messaging")
    plugin_manager.install(git_plugin)

    token = "app-token-" + str(uuid4())
    instance_data = WeaveEnvInstanceData(machine_id=machine_id, app_token=token)
    plugin_data = PluginData(app_id=url_to_plugin_id(MESSAGING_PLUGIN_URL),
                             name="WeaveServer", description="Messaging",
                             enabled=True, machine=instance_data)
    plugin_data.save(force_insert=True)
    instance_data.save(force_insert=True)


def handle_messaging_token():
    plugins_db = PluginsDatabase(os.path.join(get_config_path(), "db"))
    plugins_db.start()

    messaging_server_url = MESSAGING_PLUGIN_URL
    if sys.argv[1] == 'set':
        plugins_db.insert(app_id=url_to_plugin_id(messaging_server_url),
                          app_secret_token=sys.argv[2], is_remote=True)
    elif sys.argv[1] == 'get':
        plugin_data = plugins_db.query(url_to_plugin_id(messaging_server_url))
        print(plugin_data.app_secret_token)
    else:
        print("Supported operations: 'get' and 'set'")


def handle_weave_launch():
    plugin_dir = sys.argv[1]
    os.chdir(plugin_dir)
    sys.path.append(plugin_dir)

    token = sys.stdin.readline().strip()

    venv_path = sys.argv[2]
    venv = VirtualEnvManager(venv_path)
    venv.activate()

    raw_info = {
        "installed": True,
        "install_path": plugin_dir,
        "name": os.path.basename(plugin_dir),
    }
    plugin_info = PluginInfoFilter().filter(raw_info)

    kwargs = {"venv_dir": venv_path}

    if issubclass(plugin_info["service_cls"], MessagingEnabled):
        # Discover messaging
        kwargs["conn"] = None  # TODO: Discover for plugins except messaging.
        kwargs["auth_token"] = token

    app = plugin_info["service_cls"](**kwargs)

    signal.signal(signal.SIGTERM, lambda x, y: app.on_service_stop())
    signal.signal(signal.SIGINT, lambda x, y: app.on_service_stop())

    app.before_service_start()
    app.on_service_start()


def handle_discover():
    conn = WeaveConnection.discover()
    print(conn.default_host, conn.default_port)
