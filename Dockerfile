# syntax=docker/dockerfile:1

FROM python:3.12-slim

# we expect /home to be pristine in the running container
WORKDIR /home

RUN apt-get update
RUN apt-get install -y git

# invalidate the Docker cache from this point when requirements.txt changes
ADD https://raw.githubusercontent.com/shbatm/finitude/master/requirements.txt requirements.txt

RUN python3 -m venv pyenv

RUN /home/pyenv/bin/pip install -r requirements.txt

# invalidate the Docker cache from this point when git changes
ADD https://api.github.com/repos/shbatm/finitude/git/refs/heads/master version.json

RUN git clone https://github.com/shbatm/finitude

# we expect /var/lib/finitude might be a mount point,
# so we read our config from there
RUN mkdir /var/lib/finitude

RUN cp /home/finitude/finitude.yml /var/lib/finitude/

WORKDIR /home/finitude

CMD [ "/home/pyenv/bin/python", "-m", "finitude", "/var/lib/finitude/finitude.yml" ]
