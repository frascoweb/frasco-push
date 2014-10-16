from frasco import Feature, action, current_app
from redis import StrictRedis
from tornadopush import EventEmitter, assets


class PushFeature(Feature):
    name = 'push'
    defaults = {"redis_host": "localhost",
                "redis_port": 6379}

    def init_app(self, app):
        self.options.setdefault("secret", app.config['SECRET_KEY'])
        app.processes.append(("pusher", ["tornadopush", "--auth",
            "--secret", self.options["secret"], "--redis-host",
            self.options["redis_port"], "--redis-port", self.options["redis_port"]]))
        self.redis_client = StrictRedis(self.options["redis_host"],
            self.options["redis_port"])
        self.event_emitter = EventEmitter(self.redis_client)
        app.jinja_env.globals['create_push_token'] = self.create_token
        if app.features.exists('assets'):
            app.features.assets.expose_package('tornadopush', 'tornadopush')
            app.assets.register('tornadopush',
                map(lambda s: 'tornadopush/%s' % s, assets.asset_files))

    @action('create_push_token', default_option='user_id', as_='token')
    def create_token(self, user_id=None, allowed_channels=None):
        if user_id is None and current_app.features.exists('users'):
            user_id = str(current_app.features.users.current.id)
        return self.event_emitter.create_token(user_id, allowed_channels)

    @action('emit_push_event')
    def emit(self, channel, event, data, target=None):
        self.event_emitter.emit(channel, event, data, target)