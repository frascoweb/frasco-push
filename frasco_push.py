from frasco import Feature, action, current_app, hook
from redis import StrictRedis
from tornadopush import EventEmitter, assets
import uuid
import urlparse


class PushFeature(Feature):
    name = 'push'
    defaults = {"queue": "toredis",
                "redis_host": None,
                "redis_port": 6379,
                "server_hostname": None,
                "server_port": 8888,
                "server_secured": False,
                "auth_enabled": True,
                "create_token_for_anon": False,
                "debug": None,
                "assets_output": "tornadopush",
                "ignore_own_messages": False}

    def init_app(self, app):
        self.options.setdefault("secret", app.config['SECRET_KEY'])
        
        if not self.options['redis_host'] and 'redis' in app.features:
            r = urlparse.urlparse(app.features.redis.options['url'])
            if ':' in r.netloc:
                host, port = r.netloc.split(':')
                self.options['redis_host'] = host
                self.options['redis_port'] = port
            else:
                self.options['redis_host'] = r.netloc
        elif not self.options['redis_host']:
            self.options['redis_host'] = 'localhost'

        args = ["python", "-m", "tornadopush",
            "--secret", self.options["secret"],
            "--queue", self.options["queue"],
            "--redis-host", self.options["redis_host"],
            "--redis-port", self.options["redis_port"],
            "--port", self.options["server_port"]]
        if self.options['auth_enabled']:
            args.append('--auth')
        if self.options['ignore_own_messages']:
            args.append('--ignore-own-messages')
        if self.options['debug'] or (self.options['debug'] is None and app.debug):
            args.append("--debug")
        app.processes.append(("push", args))
        self.redis_client = StrictRedis(self.options["redis_host"],
            self.options["redis_port"])
        self.event_emitter = EventEmitter(self.redis_client, self.options['secret'])
        app.jinja_env.globals['create_push_token'] = self.create_token

        if not self.options["server_hostname"]:
            server_name = app.config.get('SERVER_NAME', 'localhost')

            self.options["server_hostname"] = "%s:%s" % (
                server_name.split(':')[0], self.options['server_port'])

        if app.features.exists('assets'):
            app.features.assets.expose_package('tornadopush', 'tornadopush')
            app.assets.register('tornadopush', {
                "contents": [{"filters": "jsmin",
                    "contents": map(lambda s: 'tornadopush/%s' % s, assets.asset_files)}],
                "output": self.options['assets_output']})

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
        if user_id is None and self.options['create_token_for_anon']:
            user_id = str(uuid.uuid4())
        if user_id is not None:
            return self.event_emitter.create_token(user_id, allowed_channels)

    @action('emit_push_event')
    def emit(self, channel, event, data=None, sender=None, target=None, serialize=True):
        self.event_emitter.emit(channel, event, data, sender, target, serialize)