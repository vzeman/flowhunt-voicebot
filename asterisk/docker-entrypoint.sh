#!/bin/sh
set -eu

: "${SIP_HOST:?missing SIP_HOST}"
: "${SIP_USER:?missing SIP_USER}"
: "${SIP_PASSWORD:?missing SIP_PASSWORD}"
: "${AUDIOSOCKET_SERVICE:?missing AUDIOSOCKET_SERVICE}"
: "${AMI_USERNAME:=voicebot}"
: "${AMI_PASSWORD:=voicebot-local-dev}"

cat >/etc/asterisk/pjsip.conf <<EOF
[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:5060

[liveagent-reg]
type=registration
transport=transport-udp
outbound_auth=liveagent-auth
server_uri=sip:${SIP_HOST}
client_uri=sip:${SIP_USER}@${SIP_HOST}
contact_user=${SIP_USER}
retry_interval=30
forbidden_retry_interval=300
expiration=300

[liveagent-auth]
type=auth
auth_type=userpass
username=${SIP_USER}
password=${SIP_PASSWORD}

[liveagent-aor]
type=aor
contact=sip:${SIP_HOST}

[liveagent-endpoint]
type=endpoint
transport=transport-udp
context=from-liveagent
disallow=all
allow=ulaw,alaw,slin
outbound_auth=liveagent-auth
aors=liveagent-aor
from_user=${SIP_USER}
from_domain=${SIP_HOST}
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
timers=no

[liveagent-identify]
type=identify
endpoint=liveagent-endpoint
match=${SIP_HOST}
EOF

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
