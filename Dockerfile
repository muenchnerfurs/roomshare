FROM pretix/standalone:2026.2

USER root

ADD . /roomshare
RUN cd /roomshare && make localecompile
RUN cd /roomshare && pip3 install -e .
RUN pip3 install pretix-fontpack-free

USER pretixuser

RUN cd /pretix/src && make production
