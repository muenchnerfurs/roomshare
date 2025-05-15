FROM pretix/standalone:2025.3.0

USER root

ADD . /roomshare
RUN cd /roomshare && pip3 install -e .
RUN pip3 install pretix-fontpack-free

USER pretixuser

RUN cd /pretix/src && make production
