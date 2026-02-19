"""Dockerfile templates for 3-tier SWE-bench image builds.

Ported from swebench.harness.dockerfiles.python — Python only.
"""

from __future__ import annotations

DOCKERFILE_BASE = r"""
FROM --platform={platform} ubuntu:{ubuntu_version}

ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

RUN apt update && apt install -y \
wget \
git \
build-essential \
libffi-dev \
libtiff-dev \
python3 \
python3-pip \
python-is-python3 \
jq \
curl \
locales \
locales-all \
tzdata \
&& rm -rf /var/lib/apt/lists/*

# Download and install conda
RUN wget 'https://repo.anaconda.com/miniconda/Miniconda3-{conda_version}-Linux-{conda_arch}.sh' -O miniconda.sh \
    && bash miniconda.sh -b -p /opt/miniconda3
# Add conda to PATH
ENV PATH=/opt/miniconda3/bin:$PATH
# Add conda to shell startup scripts like .bashrc (DO NOT REMOVE THIS)
RUN conda init --all
RUN conda config --append channels conda-forge

RUN adduser --disabled-password --gecos 'dog' nonroot
"""

DOCKERFILE_ENV = r"""FROM --platform={platform} {base_image_key}

COPY ./setup_env.sh /root/
RUN sed -i -e 's/\r$//' /root/setup_env.sh
RUN chmod +x /root/setup_env.sh
RUN /bin/bash -c "source ~/.bashrc && /root/setup_env.sh"

WORKDIR /testbed/

# Automatically activate the testbed environment
RUN echo "source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed" > /root/.bashrc
"""

DOCKERFILE_INSTANCE = r"""FROM --platform={platform} {env_image_name}

COPY ./setup_repo.sh /root/
RUN sed -i -e 's/\r$//' /root/setup_repo.sh
RUN /bin/bash /root/setup_repo.sh

WORKDIR /testbed/
"""


def get_dockerfile_base(platform: str, arch: str, **kwargs) -> str:
    conda_arch = "aarch64" if arch == "arm64" else arch
    return DOCKERFILE_BASE.format(platform=platform, conda_arch=conda_arch, **kwargs)


def get_dockerfile_env(platform: str, base_image_key: str, **kwargs) -> str:
    return DOCKERFILE_ENV.format(platform=platform, base_image_key=base_image_key, **kwargs)


def get_dockerfile_instance(platform: str, env_image_name: str) -> str:
    return DOCKERFILE_INSTANCE.format(platform=platform, env_image_name=env_image_name)
