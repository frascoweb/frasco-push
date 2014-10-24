from frasco import Feature, action, current_app, hook
from redis import StrictRedis
from tornadopush import EventEmitter, assets


class PushFeature(Feature):
    name = 'push'
    defaults = {"redis_host": "localhost",
                "redis_port": 6379,
                "server_hostname": None,
                "server_port": 8888,
                "server_secured": False}

    def init_app(self, app):
        self.options.setdefault("secret", app.config['SECRET_KEY'])
        args = ["python", "-m", "tornadopush", "--auth",
            "--secret", self.options["secret"],
            "--redis-host", self.options["redis_host"],
            "--redis-port", self.options["redis_port"],
            "--port", self.options["server_port"]]
        if app.debug:
            args.append("--debug")
        app.processes.append(("push", args))
        self.redis_client = StrictRedis(self.options["redis_host"],
            self.options["redis_port"])
        self.event_emitter = EventEmitter(self.redis_client, self.options['secret'])
        app.jinja_env.globals['create_push_token'] = self.create_token

        if not self.options["server_hostname"]:
            self.options["server_hostname"] = "%s:%s" % (
                app.config['SERVER_NAME'] or 'localhost',
                self.options['server_port'])

        if app.features.exists('assets'):
            app.features.assets.expose_package('tornadopush', 'tornadopush')
            app.assets.register('tornadopush', {
                "contents": map(lambda s: 'tornadopush/%s' % s, assets.asset_files),
                "output": "tornadopush"})

    @hook()
    def before_request(self):
        current_app.config['EXPORTED_JS_VARS']['TORNADOPUSH_HOSTNAME'] = \
            self.options['server_hostname']
        current_app.config['EXPORTED_JS_VARS']['TORNADOPUSH_SECURED'] = \
            self.options['server_secured']
        token = self.create_token()
        if token:
            current_app.config['EXPORTED_JS_VARS']['TORNADOPUSH_TOKEN'] = token

    @action('create_push_token', default_option='user_id', as_='token')
    def create_token(self, user_id=None, allowed_channels=None):
        if user_id is None and current_app.features.exists('users') and \
          current_app.features.users.logged_in():
            user_id = str(current_app.features.users.current.id)
        if user_id:
            return self.event_emitter.create_token(user_id, allowed_channels)

    @action('emit_push_event')
    def emit(self, channel, event, data, target=None):
        self.event_emitter.emit(channel, event, data, target)