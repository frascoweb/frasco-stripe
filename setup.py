from setuptools import setup, find_packages


setup(
    name='frasco-stripe',
    version='0.4.1',
    url='http://github.com/frascoweb/frasco-stripe',
    license='MIT',
    author='Maxime Bouroumeau-Fuseau',
    author_email='maxime.bouroumeau@gmail.com',
    description="Stripe integration for Frasco",
    packages=find_packages(),
    zip_safe=False,
    platforms='any',
    install_requires=[
        'frasco',
        'frasco-models',
        'stripe'
    ]
)
