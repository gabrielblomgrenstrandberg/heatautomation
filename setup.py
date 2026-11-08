# Heat Automation; A program that selects the best heat source based on spot price and outdoor temperature
# Copyright (C) 2025  Gabriel Blomgren Strandberg <gabriel.blomgren.strandberg@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

# setup.py - This script is used to package the Python project for distribution.

from setuptools import setup, find_namespace_packages

setup(
    name='heatautomation',
    version='0.1.0',
    author='Gabriel Blomgren Strandberg',
    author_email='gabriel.blomgren.strandberg@gmail.com',
    description='Automatic heat source selector that selects the best heat source based on spot price and outoor temperature.',
    packages=find_namespace_packages(include=['heatautomation', 'heatautomation.*']),
    package_data={'heatautomation': ['heatautomation.conf']},
    install_requires=[
        'requests',
        'python-dotenv',
        'websocket-client',
        'selenium',
        'webdriver-manager',
        'numpy',
        'pandas',
        'scikit-learn',
        'SQLAlchemy',
        'alembic',
        'pydantic>=2,<3',
        'joblib',
        'scipy>=1.9; python_version >= "3.8"',
    ],
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: GNU Affero General Public License v3',
        'Operating System :: OS Independent',
    ],
    python_requires='>=3.9',
    license='AGPL-3.0-or-later',
)
