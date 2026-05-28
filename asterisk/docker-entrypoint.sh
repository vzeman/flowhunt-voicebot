#!/bin/sh
set -eu

: "${AUDIOSOCKET_SERVICE:?missing AUDIOSOCKET_SERVICE}"
: "${AMI_USERNAME:=voicebot}"
: "${AMI_PASSWORD:=voicebot-local-dev}"
: "${PJSIP_DYNAMIC_INCLUDE:=/data/asterisk/pjsip-trunks.conf}"

mkdir -p "$(dirname "$PJSIP_DYNAMIC_INCLUDE")"

cat >/etc/asterisk/pjsip.conf <<EOF
[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:5060

#include ${PJSIP_DYNAMIC_INCLUDE}
EOF

if [ ! -s "$PJSIP_DYNAMIC_INCLUDE" ]; then
  if [ -n "${SIP_HOST:-}" ] && [ -n "${SIP_USER:-}" ] && [ -n "${SIP_PASSWORD:-}" ]; then
    cat >"$PJSIP_DYNAMIC_INCLUDE" <<EOF
; Seeded from SIP_HOST/SIP_USER/SIP_PASSWORD for local development.
[trunk-default-reg]
type=registration
transport=transport-udp
outbound_auth=trunk-default-auth
server_uri=sip:${SIP_HOST}
client_uri=sip:${SIP_USER}@${SIP_HOST}
contact_user=${SIP_USER}
endpoint=trunk-default-endpoint
line=yes
retry_interval=30
forbidden_retry_interval=300
expiration=300

[trunk-default-auth]
type=auth
auth_type=userpass
username=${SIP_USER}
password=${SIP_PASSWORD}

[trunk-default-aor]
type=aor
contact=sip:${SIP_HOST}

[trunk-default-endpoint]
type=endpoint
transport=transport-udp
context=from-liveagent
disallow=all
allow=ulaw,alaw,slin
outbound_auth=trunk-default-auth
aors=trunk-default-aor
from_user=${SIP_USER}
from_domain=${SIP_HOST}
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
timers=no

[trunk-default-identify]
type=identify
endpoint=trunk-default-endpoint
match=${SIP_HOST}
EOF
  else
    printf '; Dynamic SIP trunks are managed by the voicebot API.\n' >"$PJSIP_DYNAMIC_INCLUDE"
  fi
fi

cat >/etc/asterisk/manager.conf <<EOF
[general]
enabled=yes
webenabled=no
port=5038
bindaddr=0.0.0.0

[${AMI_USERNAME}]
secret=${AMI_PASSWORD}
read=system,call,command,agent,user,config,dtmf,reporting,cdr,dialplan
write=system,call,command,agent,user,config,originate,reporting
EOF

sed "s#__AUDIOSOCKET_SERVICE__#${AUDIOSOCKET_SERVICE}#g" \
  /templates/extensions.conf >/etc/asterisk/extensions.conf

exec asterisk -f -vvv
