import os
import sys
import logging

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "lib"))

import xbmc  # noqa: E402

from tgkodi import common  # noqa: E402

common.setup_native_crypto()  # must happen before telethon is imported

from tgkodi.server import Engine, KodiLogHandler  # noqa: E402


def read_config():
    return {
        "api_id": common.setting("api_id").strip(),
        "api_hash": common.setting("api_hash").strip(),
        "port": common.port(),
        "include_channels": common.setting_bool("include_channels", True),
        "include_groups": common.setting_bool("include_groups", True),
        "include_private": common.setting_bool("include_private", True),
        "page_size": common.setting_int("page_size", 50, 10, 200),
        "cache_minutes": common.setting_int("cache_minutes", 10, 1, 1440),
        "parallel": common.setting_int("parallel", 4, 1, 8),
        "cache_mb": common.setting_int("cache_mb", 500, 20, 100000),
    }


class Monitor(xbmc.Monitor):
    def __init__(self, engine):
        xbmc.Monitor.__init__(self)
        self.engine = engine

    def onSettingsChanged(self):
        common.log("settings changed")
        self.engine.update_config(read_config())


def main():
    handler = KodiLogHandler(lambda m: common.log(m, xbmc.LOGDEBUG))
    tl = logging.getLogger("telethon")
    tl.addHandler(handler)
    tl.setLevel(logging.INFO)

    engine = Engine(common.profile_dir(), common.get_token(create=True), common.log)
    try:
        engine.start(read_config())
    except Exception as e:
        common.log("engine failed to start: %r" % e, xbmc.LOGERROR)
        return
    common.log("service started (python %s)" % sys.version.split()[0])
    monitor = Monitor(engine)
    while not monitor.abortRequested():
        if monitor.waitForAbort(5):
            break
    engine.stop()
    common.log("service stopped")


main()
