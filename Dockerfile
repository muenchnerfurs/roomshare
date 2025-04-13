FROM pretix/standalone:2025.3.0

USER root

ADD roomshare /roomshare
RUN cd /roomshare && pip3 install -e .

USER pretixuser

RUN cd /pretix/src && make production
