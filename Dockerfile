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

# Dockerfile for the Heat Automation project

# Build the frontend separately; Node is not needed in the runtime image.
FROM node:22 AS web-build
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/index.html frontend/tsconfig.json frontend/vite.config.ts frontend/tailwind.config.js frontend/postcss.config.js ./
COPY frontend/src ./src
RUN npm run build

# Use RHEL 9 Universal Base Image minimal
FROM registry.access.redhat.com/ubi9/ubi-minimal

# Install necessary system packages and fonts for Chromium and Selenium
RUN microdnf update -y && \
    microdnf install -y \
        python3 \
        python3-pip \
        wget \
        unzip \
        dbus-glib \
        libXScrnSaver \
        alsa-lib \
        ipa-gothic-fonts \
        liberation-fonts \
        xorg-x11-fonts-misc \
        xorg-x11-fonts-100dpi \
        xorg-x11-fonts-75dpi \
        xorg-x11-fonts-Type1 \
        fontconfig \
        mesa-dri-drivers \
        mesa-libGL \
        mesa-libgbm \
        nss \
        alsa-lib \
        atk \
        cups-libs \
        libXcomposite \
        libXcursor \
        libXdamage \
        libXext \
        libXi \
        libXtst \
        pango \
        gtk3 \
        glib2 \
    && microdnf clean all

# Download and install Chromium (version 116.0.5845.140)
RUN wget https://github.com/alrra/browser-downloads/releases/download/chromium-116.0.5845.140-rpm/chromium-116.0.5845.140-1.el9.x86_64.rpm && \
    microdnf install -y chromium-116.0.5845.140-1.el9.x86_64.rpm && \
    rm chromium-116.0.5845.140-1.el9.x86_64.rpm

# Download matching Chromedriver (version 116.0.5845.96)
RUN wget https://chromedriver.storage.googleapis.com/116.0.5845.96/chromedriver_linux64.zip && \
    unzip chromedriver_linux64.zip && \
    mv chromedriver /usr/local/bin/chromedriver && \
    chmod +x /usr/local/bin/chromedriver && \
    rm chromedriver_linux64.zip

# Set environment variable for chromium binary path
ENV CHROME_BINARY=/usr/bin/chromium
ENV PATH=/usr/local/bin:$PATH

# Set working directory and copy your app code
WORKDIR /app
COPY . /app
COPY --from=web-build /web/dist /app/frontend/dist

# Install python dependencies
RUN python3 -m pip install --no-cache-dir -r requirements.txt -r requirements-web.txt

# Default command to run your app
CMD ["python3", "-m", "heatautomation.main"]
