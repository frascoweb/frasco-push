from frasco import Feature, action, current_app, hook, request, command, current_context
from flask import has_request_context
import socketio
import os
import urlparse
import uuid
import json
import hashlib
from itsdangerous import URLSafeTimedSerializer, BadSignature


class PushFeature(Feature):
    name = 'push'
    command_group = False
    defaults = {"redis_url": None,
                "server_url": None,
                "server_port": 8888,
                "server_secured": False,
                "channel": "socketio",
                "sio_client_version": "1.4.5",
                "skip_self": False,
                "secret": None,
                "prefix_event_with_room": True,
                "default_current_user_handler": True}

    def init_app(self, app):
        if self.options['secret'] is None:
            self.options["secret"] = app.config['SECRET_KEY']
        
        if not self.options['redis_url'] and 'redis' in app.features:
            self.options['redis_url'] = app.features.redis.options['url']

        args = ["python", "-m", "frasco_push",
            "--channel", self.options["channel"],
            "--redis", self.options["redis_url"],
            "--port", self.options["server_port"]]
        if self.options['secret']:
            args.extend(["--secret", self.options["secret"]])
        app.processes.append(("push", args))

        if not self.options["server_url"]:
            server_name = app.config.get('SERVER_NAME') or 'localhost'
            self.options["server_url"] = "%s://%s:%s" % (
                "https" if self.options['server_secured'] else "http",
                server_name.split(':')[0], self.options['server_port'])

        if app.features.exists('assets'):
            app.assets.register('socketio', [
                'https://cdn.socket.io/socket.io-%s.js' % self.options['sio_client_version']])

        self.token_serializer = URLSafeTimedSerializer(self.options['secret'])
        self.manager = socketio.RedisManager(self.options['redis_url'],
            channel=self.options['channel'], write_only=True)

        self.current_user_handler = None
        if 'users' in app.features and self.options['default_current_user_handler']:
            self.current_user_handler = self.default_current_user_handler

    def current_user(self, func):
        self.current_user_handler = func
        return func

    def default_current_user_handler(self):
        if not current_app.features.users.logged_in():
            return None, {"guest": True}, None
        info = {"guest": False}
        current = current_app.features.users.current
        allowed_rooms = None
        if hasattr(current, 'get_allowed_push_rooms'):
            allowed_rooms = current.get_allowed_push_rooms()
        if hasattr(current, 'for_json'):
            return current.get_id(), current.for_json(), allowed_rooms
        if current_app.features.users.options['username_column'] != current_app.features.users.options['email_column']:
            info['username'] = getattr(current, current_app.features.users.options['username_column'])
        if 'users_avatar' in current_app.features:
            info['avatar_url'] = current.avatar_url
        return current.get_id(), info, allowed_rooms

    def get_direct_event(self, user_id):
        if not self.options['secret']:
            raise Exception('A secret must be set to use emit_direct()')
        return hashlib.sha1(str(user_id) + self.options['secret']).hexdigest()

    @hook()
    def before_request(self):
        if self.options['secret']:
            user_id = None
            user_info = None
            allowed_rooms = None
            if self.current_user_handler:
                user_id, user_info, allowed_rooms = self.current_user_handler()
            current_context['socketio_token'] = self.create_token(user_info, allowed_rooms)
            if user_id:
                current_context['socketio_user_event'] = self.get_direct_event(user_id)

        if current_app.features.exists('assets'):
            current_app.config['EXPORTED_JS_VARS'].update({
                'SOCKETIO_URL': self.options['server_url'],
                'SOCKETIO_TOKEN': current_context.get('socketio_token'),
                'SOCKETIO_USER_EVENT': current_context.get('socketio_user_event')
            })

    @command('emit_push_event')
    @action('emit_push_event')
    def emit(self, event, data=None, skip_self=None, room=None, **kwargs):
        if self.options['prefix_event_with_room'] and room:
            event = "%s:%s" % (room, event)
        if skip_self is None:
            skip_self = self.options['skip_self']
        if skip_self and has_request_context() and 'x-socketio-sid' in request.headers:
            kwargs['skip_sid'] = request.headers['x-socketio-sid']
        return self.manager.emit(event, data=data, room=room, **kwargs)

    def emit_to_user(self, user_id, data=None, **kwargs):
        return self.emit(self.get_direct_event(user_id), data=data, **kwargs)

    @action('create_push_token', default_option='user_info', as_='token')
    def create_token(self, user_info=None, allowed_rooms=None):
        return self.token_serializer.dumps([user_info, allowed_rooms])


class PresenceRedisManager(socketio.RedisManager):
    def __init__(self, *args, **kwargs):
        self.presence_session_id = kwargs.pop('presence_session_id', str(uuid.uuid4()).split('-')[-1])
        self.presence_key_prefix = "presence%s:" % self.presence_session_id
        super(PresenceRedisManager, self).__init__(*args, **kwargs)

    def enter_room(self, sid, namespace, room):
        super(PresenceRedisManager, self).enter_room(sid, namespace, room)
        if room and room != sid:
            self.redis.sadd("%s%s:%s" % (self.presence_key_prefix, namespace, room), sid)
            self.server.emit('%s:joined' % room, {"sid": sid, "info": self.get_member_info(sid, namespace)},
                room=room, skip_sid=sid)

    def leave_room(self, sid, namespace, room):
        super(PresenceRedisManager, self).leave_room(sid, namespace, room)
        if room and room != sid:
            self.redis.srem("%s%s:%s" % (self.presence_key_prefix, namespace, room), sid)
            self.server.emit('%s:left' % room, sid, room=room, skip_sid=sid)

    def get_room_members(self, namespace, room):
        return self.redis.smembers("%s%s:%s" % (self.presence_key_prefix, namespace, room))

    def set_member_info(self, sid, namespace, info):
        self.redis.set("%s%s@%s" % (self.presence_key_prefix, namespace, sid), json.dumps(info))
        for room in self.get_rooms(sid, namespace):
            if not room or room == sid:
                continue
            self.server.emit('%s:member_updated' % room, {"sid": sid, "info": info}, room=room, skip_sid=sid)

    def get_member_info(self, sid, namespace):
        data = self.redis.get("%s%s@%s" % (self.presence_key_prefix, namespace, sid))
        if data:
            try:
                return json.loads(data)
            except:
                pass
        return {}

    def disconnect(self, sid, namespace):
        super(PresenceRedisManager, self).disconnect(sid, namespace)
        self.redis.delete("%s%s@%s" % (self.presence_key_prefix, namespace, sid))

    def cleanup_presence_keys(self):
        keys = self.redis.keys('%s*' % self.presence_key_prefix)
        pipe = self.redis.pipeline()
        for key in keys:
            pipe.delete(key)
        pipe.execute()


def create_app(redis_url='redis://', channel='socketio', secret=None, token_max_age=None):
    mgr = PresenceRedisManager(redis_url, channel=channel)
    sio = socketio.Server(client_manager=mgr, async_mode='eventlet')
    token_serializer = URLSafeTimedSerializer(secret)
    default_ns = '/'

    @sio.on('connect')
    def connect(sid, env):
        if not secret:
            return
        try:
            qs = urlparse.parse_qs(env['QUERY_STRING'])
            if not 'token' in qs:
                return False
            user_info, allowed_rooms = token_serializer.loads(qs['token'][0], max_age=token_max_age)
            env['allowed_rooms'] = allowed_rooms
            if user_info:
                mgr.set_member_info(sid, default_ns, user_info)
        except BadSignature as e:
            return False

    @sio.on('members')
    def get_room_members(sid, data):
        if not data.get('room') or data['room'] not in mgr.get_rooms(sid, default_ns):
            return []
        return {sid: mgr.get_member_info(sid, default_ns) for sid in mgr.get_room_members(default_ns, data['room'])}

    @sio.on('join')
    def join(sid, data):
        if sio.environ[sid].get('allowed_rooms') and data['room'] not in sio.environ[sid]['allowed_rooms']:
            return False
        sio.enter_room(sid, data['room'])
        return get_room_members(sid, data)

    @sio.on('broadcast')
    def room_broadcast(sid, data):
        sio.emit("%s:%s" % (data['room'], data['event']), data.get('data'), room=data['room'], skip_sid=sid)

    @sio.on('leave')
    def leave(sid, data):
        sio.leave_room(sid, data['room'])

    @sio.on('set')
    def set(sid, data):
        mgr.set_member_info(sid, default_ns, data)

    @sio.on('get')
    def get(sid, data):
        return mgr.get_member_info(data['sid'], default_ns)

    return socketio.Middleware(sio)


def _get_env_var(wsgi_env, name, default=None):
    return wsgi_env.get(name, os.environ.get(name, default))


_wsgi_app = None
def wsgi_app(environ, start_response):
    global _wsgi_app
    if not _wsgi_app:
        _wsgi_app = create_app(_get_env_var(environ, 'SIO_REDIS_URL', 'redis://'),
            _get_env_var(environ, 'SIO_CHANNEL', 'socketio'), _get_env_var(environ, 'SIO_SECRET'))
    return _wsgi_app(environ, start_response)


def cleanup_wsgi_app():
    if _wsgi_app:
        _wsgi_app.engineio_app.manager.cleanup_presence_keys()


def run_server(port=8888, **kwargs):
    from eventlet import wsgi
    import eventlet
    eventlet.monkey_patch()
    env = dict([("SIO_%s" % k.upper(), v) for k, v in kwargs.items()])
    wsgi.server(eventlet.listen(('', port)), wsgi_app, environ=env)
    cleanup_wsgi_app()


if __name__ == '__main__':
    import argparse
    argparser = argparse.ArgumentParser(prog='tornadopush',
        description='Start tornadopush server')
    argparser.add_argument('-p', '--port', default=8888, type=int,
        help='Port number')
    argparser.add_argument('-r', '--redis', default=os.environ.get('SIO_REDIS_URL', 'redis://'), type=str,
        help='Redis URL')
    argparser.add_argument('-c', '--channel', default=os.environ.get('SIO_CHANNEL', 'socketio'), type=str,
        help='Channel')
    argparser.add_argument('-s', '--secret', default=os.environ.get('SIO_SECRET'), type=str,
        help='Secret')
    args = argparser.parse_args()
    run_server(args.port, redis_url=args.redis, channel=args.channel, secret=args.secret)