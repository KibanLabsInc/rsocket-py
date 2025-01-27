from setuptools import setup, find_packages

with open('README.md') as fd:
    long_description = fd.read()

setup(
    name='rsocket',
    version='0.3',
    description='Python RSocket library',
    long_description=long_description,
    long_description_content_type='text/markdown',
    url='https://github.com/rsocket/rsocket-py',
    author='Gabriel Shaar',
    author_email='gabis@precog.co',
    license='MIT',
    packages=find_packages(exclude=['examples', 'tests', 'tests.*', 'docs']),
    zip_safe=True,
    python_requires='>=3.8',
    extras_require={
        'rx': {'Rx >= 3.0.0'},
        'aiohttp': {'aiohttp >= 3.0.0'},
        'quart': {'quart >= 0.15.0'},
        'quic': {'aioquic >= 0.9.0'}
    },
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Environment :: Web Environment',
        'License :: OSI Approved :: MIT License',
        'Natural Language :: English',
        'Topic :: Internet :: WWW/HTTP :: Dynamic Content',
        'Topic :: Internet :: WWW/HTTP',
        'Topic :: Internet',
        'Topic :: Software Development :: Libraries :: Application Frameworks',
        'Topic :: Software Development :: Libraries :: Python Modules',
    ])
