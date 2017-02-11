from setuptools import setup, find_packages


setup(
    name='frasco-push',
    version='0.3.2',
    url='http://github.com/frascoweb/frasco-push',
    license='MIT',
    author='Maxime Bouroumeau-Fuseau',
    author_email='maxime.bouroumeau@gmail.com',
    description="Socket.IO integration from Frasco",
    py_modules=["frasco_push"],
    zip_safe=False,
    platforms='any',
    install_requires=[
        'frasco',
        'redis',
        'python-socketio',
        'eventlet'
    ]
)
