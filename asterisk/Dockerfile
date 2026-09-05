# Alpine, not Debian: Asterisk was dropped from Debian bookworm's repositories,
# so a bookworm base cannot install it at all. Alpine carries it in community
# and the image is a fraction of the size.
FROM alpine:3.20

RUN apk add --no-cache \
      asterisk \
      asterisk-sample-config \
      asterisk-sounds-en \
      espeak-ng \
      curl \
      bash

# Sounds staged by the application land here. The name must match
# ASTERISK_SOUNDS_DIR in the backend.
RUN mkdir -p /var/lib/asterisk/sounds/crp \
 && chown -R asterisk:asterisk /var/lib/asterisk/sounds/crp /var/lib/asterisk /var/log/asterisk

# Our config is layered OVER the distro sample set rather than replacing the
# directory. Asterisk needs dozens of files we have no opinion about; mounting a
# volume over /etc/asterisk would remove them and the daemon would not start.
COPY conf/ari.conf conf/http.conf conf/pjsip.conf conf/extensions.conf      conf/cdr.conf conf/modules.conf /etc/asterisk/

HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=6 \
  CMD asterisk -rx "core show uptime" > /dev/null 2>&1 || exit 1

CMD ["asterisk", "-f", "-vvv", "-U", "asterisk"]
