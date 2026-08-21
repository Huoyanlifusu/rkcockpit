#!/bin/sh
# Atomic rkss-portal installer for Debian/Ubuntu (Python 3.9+ stdlib).
#   ./install.sh portal --user operator
#   ./install.sh portal --user operator --tls-nginx portal.example.com /path/fullchain.pem /path/key.pem
set -eu
umask 077

SRC=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP_ROOT=${RKSS_APP_ROOT:-/opt/rkss-webui}
RELEASES=$APP_ROOT/releases
CURRENT=$APP_ROOT/current
LAST_GOOD=$APP_ROOT/last-good
SYSTEMD_DIR=${RKSS_SYSTEMD_DIR:-/etc/systemd/system}
AUTH_DIR=${RKSS_AUTH_DIR:-/etc/rkss-portal}
RUN_USER=${RKSS_RUN_USER:-}
TOKEN_FILE=$AUTH_DIR/auth-token
ENV_FILE=$AUTH_DIR/portal.env
NGINX_AVAILABLE=${RKSS_NGINX_AVAILABLE:-/etc/nginx/sites-available}
NGINX_ENABLED=${RKSS_NGINX_ENABLED:-/etc/nginx/sites-enabled}
UNIT=$SYSTEMD_DIR/rkss-portal.service
RELEASE_ID=${RKSS_RELEASE_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}

fail() { echo "install: $*" >&2; exit 1; }
usage() { fail "usage: $0 portal [--user USER] [--tls-nginx SERVER_NAME CERT KEY]"; }
escape_sed() { printf '%s' "$1" | sed 's/[\\&|]/\\&/g'; }
secure_root_dir() {
    path=$1
    case "$path" in /*) ;; *) fail "managed directory must be absolute: $path";; esac
    current=
    old_ifs=$IFS
    IFS=/
    for component in $path; do
        [ -n "$component" ] || continue
        current=$current/$component
        [ ! -L "$current" ] || fail "managed directory must not contain a symlink: $current"
        if [ -e "$current" ]; then
            [ -d "$current" ] || fail "managed path is not a directory: $current"
            [ "$(stat -c %u -- "$current")" -eq 0 ] || fail "managed directory must be owned by root: $current"
            dir_mode=$(stat -c %a -- "$current")
            [ "$((0$dir_mode & 022))" -eq 0 ] || fail "managed directory must not be group/other writable: $current"
        else
            mkdir "$current"
            chmod 755 "$current"
        fi
    done
    IFS=$old_ifs
}
atomic_link() {
    target=$1 link=$2 temporary=$2.tmp.$$
    ln -s "$target" "$temporary"
    mv -Tf "$temporary" "$link"
}

case "$APP_ROOT" in
    /|/home|/usr|/etc|/opt|*/../*|*/..|*/./*|*/.)
        fail "unsafe application root: $APP_ROOT" ;;
    /*) ;;
    *) fail "application root must be absolute: $APP_ROOT" ;;
esac
[ "$(id -u)" -eq 0 ] || fail "run as root: sudo $0 $*"
[ "${1:-}" = portal ] || usage
shift

TLS=0
SERVER_NAME=
CERT_FILE=
KEY_FILE=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --user)
            [ "$#" -ge 2 ] || fail "--user requires USER"
            RUN_USER=$2
            shift 2
            ;;
        --tls-nginx)
            [ "$TLS" -eq 0 ] || fail "--tls-nginx may only be specified once"
            [ "$#" -ge 4 ] || fail "--tls-nginx requires SERVER_NAME CERT KEY"
            TLS=1 SERVER_NAME=$2 CERT_FILE=$3 KEY_FILE=$4
            shift 4
            ;;
        *) usage ;;
    esac
done

if [ -z "$RUN_USER" ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    RUN_USER=$SUDO_USER
fi
if [ -z "$RUN_USER" ]; then
    SOURCE_OWNER=$(stat -c %U -- "$SRC")
    if [ "$SOURCE_OWNER" != root ] && id "$SOURCE_OWNER" >/dev/null 2>&1; then
        RUN_USER=$SOURCE_OWNER
    fi
fi
[ -n "$RUN_USER" ] || fail "cannot select a non-root service user; pass --user USER"
[ "$RUN_USER" != root ] || fail "service user must not be root"
id "$RUN_USER" >/dev/null 2>&1 || fail "service user does not exist: $RUN_USER"
RUN_GROUP=$(id -gn "$RUN_USER")
RUN_HOME=$(getent passwd "$RUN_USER" | cut -d: -f6)
[ -n "$RUN_HOME" ] || fail "service user has no home directory: $RUN_USER"
case "$RUN_USER:$RUN_GROUP" in *[!A-Za-z0-9_.:@+-]*) fail "unsafe service user or group";; esac
case "$RUN_HOME" in /*) ;; *) fail "service user home must be absolute: $RUN_HOME";; esac
CONF=${RKSS_CONF_DIR:-$RUN_HOME/.rkss}
case "$CONF" in /*) ;; *) fail "configuration directory must be absolute: $CONF";; esac

case "$RELEASE_ID" in *[!A-Za-z0-9._-]*|'') fail "unsafe release id";; esac
if [ "$TLS" -eq 1 ]; then
    case "$SERVER_NAME" in -*|*[!A-Za-z0-9.-]*|'') fail "invalid TLS server name";; esac
    case "$CERT_FILE:$KEY_FILE" in *[!A-Za-z0-9_./:-]*) fail "unsafe certificate path";; esac
    [ "${CERT_FILE#/}" != "$CERT_FILE" ] || fail "certificate path must be absolute"
    [ "${KEY_FILE#/}" != "$KEY_FILE" ] || fail "private-key path must be absolute"
    [ -f "$CERT_FILE" ] || fail "certificate does not exist: $CERT_FILE"
    [ -f "$KEY_FILE" ] || fail "private key does not exist: $KEY_FILE"
    [ "$(stat -c %u "$KEY_FILE")" -eq 0 ] || fail "private key must be owned by root"
    KEY_MODE=$(stat -c %a "$KEY_FILE")
    [ "$((0$KEY_MODE & 077))" -eq 0 ] || fail "private key must not be accessible by group or others"
    command -v nginx >/dev/null 2>&1 || fail "nginx is required for --tls-nginx"
    command -v openssl >/dev/null 2>&1 || fail "openssl is required for TLS validation"
    openssl x509 -in "$CERT_FILE" -noout -checkend 0 >/dev/null || fail "certificate is expired or invalid"
    openssl x509 -in "$CERT_FILE" -noout -checkhost "$SERVER_NAME" >/dev/null || fail "certificate does not match server name"
    cert_pub=$AUTH_DIR/.cert-pub.$$
    key_pub=$AUTH_DIR/.key-pub.$$
fi

secure_root_dir "$APP_ROOT"
secure_root_dir "$RELEASES"
secure_root_dir "$SYSTEMD_DIR"
python3 "$SRC/deploy/create_auth_token.py" --dir "$AUTH_DIR" --user "$RUN_USER"
if [ -L "$CONF" ]; then fail "configuration directory must not be a symlink: $CONF"; fi
if [ -e "$CONF" ]; then
    [ -d "$CONF" ] || fail "configuration path is not a directory: $CONF"
else
    install -d -m 700 -o "$RUN_USER" -g "$RUN_GROUP" "$CONF"
fi

STAGE=$RELEASES/.staging-$RELEASE_ID-$$
NEW_RELEASE=$RELEASES/$RELEASE_ID
COMMITTED=0
OLD_CURRENT=
UNIT_BACKUP=$AUTH_DIR/.unit-backup.$$
RENDERED_UNIT=$AUTH_DIR/.unit-rendered.$$
ENV_BACKUP=$AUTH_DIR/.env-backup.$$
NGINX_CONF=$NGINX_AVAILABLE/rkss-portal.conf
NGINX_LINK=$NGINX_ENABLED/rkss-portal.conf
NGINX_BACKUP=$AUTH_DIR/.nginx-backup.$$
HAD_UNIT=0
HAD_ENV=0
HAD_NGINX=0
UNIT_TOUCHED=0
ENV_TOUCHED=0
NGINX_TOUCHED=0
NGINX_RELOADED=0
CURRENT_SWITCHED=0
SYSTEMD_RELOADED=0
SERVICE_RESTART_ATTEMPTED=0
ENABLE_TOUCHED=0
SERVICE_WAS_ENABLED=0
SERVICE_WAS_ACTIVE=0
[ ! -e "$NEW_RELEASE" ] || fail "release already exists: $NEW_RELEASE"
cleanup() {
    status=$?
    trap - EXIT
    if [ "${COMMITTED:-0}" -ne 1 ]; then
        if [ "${SERVICE_RESTART_ATTEMPTED:-0}" -eq 1 ]; then
            systemctl stop rkss-portal.service || true
        fi
        if [ "${CURRENT_SWITCHED:-0}" -eq 1 ]; then
            if [ -n "${OLD_CURRENT:-}" ]; then atomic_link "$OLD_CURRENT" "$CURRENT" || true; else rm -f "$CURRENT"; fi
        fi
        if [ "${UNIT_TOUCHED:-0}" -eq 1 ]; then
            if [ "${HAD_UNIT:-0}" -eq 1 ]; then cp -p "$UNIT_BACKUP" "$UNIT" || true; else rm -f "$UNIT"; fi
        fi
        if [ "${ENV_TOUCHED:-0}" -eq 1 ]; then
            if [ "${HAD_ENV:-0}" -eq 1 ]; then cp -p "$ENV_BACKUP" "$ENV_FILE" || true; else rm -f "$ENV_FILE"; fi
        fi
        if [ "${NGINX_TOUCHED:-0}" -eq 1 ]; then
            if [ "${HAD_NGINX:-0}" -eq 1 ]; then cp -p "$NGINX_BACKUP" "$NGINX_CONF" || true; else rm -f "$NGINX_CONF" "$NGINX_LINK"; fi
            if [ "${NGINX_RELOADED:-0}" -eq 1 ]; then nginx -s reload || true; fi
        fi
        if [ "${SYSTEMD_RELOADED:-0}" -eq 1 ]; then systemctl daemon-reload || true; fi
        if [ "${SERVICE_RESTART_ATTEMPTED:-0}" -eq 1 ] && [ "${SERVICE_WAS_ACTIVE:-0}" -eq 1 ]; then
            systemctl restart rkss-portal.service || true
        fi
        if [ "${ENABLE_TOUCHED:-0}" -eq 1 ] && [ "${SERVICE_WAS_ENABLED:-0}" -eq 0 ]; then
            systemctl disable rkss-portal.service || true
        fi
        rm -rf "$NEW_RELEASE"
    fi
    rm -rf "$STAGE"
    rm -f "${cert_pub:-}" "${key_pub:-}" "$CURRENT.tmp.$$" \
        "$LAST_GOOD.tmp.$$" "${UNIT_BACKUP:-}" "${ENV_BACKUP:-}" \
        "${NGINX_BACKUP:-}" "${NGINX_CONF:-}.new.$$" \
        "${ENV_FILE:-}.new.$$" "${RENDERED_UNIT:-}" || true
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
install -d -m 755 "$STAGE"
cp -R "$SRC/host" "$SRC/portal" "$SRC/static" "$STAGE/"
find "$STAGE" -type d -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root "$STAGE"
chmod -R a+rX "$STAGE"
mv "$STAGE" "$NEW_RELEASE"

if [ -L "$CURRENT" ]; then OLD_CURRENT=$(readlink -f "$CURRENT"); fi
[ ! -L "$UNIT" ] || fail "systemd unit must not be a symlink: $UNIT"
[ ! -L "$ENV_FILE" ] || fail "environment file must not be a symlink: $ENV_FILE"
if [ -f "$UNIT" ]; then cp -p "$UNIT" "$UNIT_BACKUP"; HAD_UNIT=1; fi
if [ -f "$ENV_FILE" ]; then cp -p "$ENV_FILE" "$ENV_BACKUP"; HAD_ENV=1; fi
if systemctl is-enabled --quiet rkss-portal.service >/dev/null 2>&1; then
    SERVICE_WAS_ENABLED=1
fi
if systemctl is-active --quiet rkss-portal.service >/dev/null 2>&1; then
    SERVICE_WAS_ACTIVE=1
fi
if [ "$TLS" -eq 1 ]; then
    openssl x509 -in "$CERT_FILE" -pubkey -noout >"$cert_pub"
    openssl pkey -in "$KEY_FILE" -pubout >"$key_pub"
    cmp -s "$cert_pub" "$key_pub" || fail "certificate and private key do not match"
    secure_root_dir "$NGINX_AVAILABLE"
    secure_root_dir "$NGINX_ENABLED"
    [ ! -L "$NGINX_CONF" ] || fail "nginx configuration must not be a symlink: $NGINX_CONF"
    if [ -f "$NGINX_CONF" ]; then cp -p "$NGINX_CONF" "$NGINX_BACKUP"; HAD_NGINX=1; fi
    sed -e "s|@@SERVER_NAME@@|$SERVER_NAME|g" \
        -e "s|@@CERT_FILE@@|$CERT_FILE|g" \
        -e "s|@@KEY_FILE@@|$KEY_FILE|g" \
        "$SRC/deploy/nginx-rkss-portal.conf" >"$NGINX_CONF.new.$$"
    NGINX_TOUCHED=1
    install -m 644 "$NGINX_CONF.new.$$" "$NGINX_CONF"
    rm -f "$NGINX_CONF.new.$$"
    atomic_link "$NGINX_CONF" "$NGINX_LINK"
    nginx -t || fail "nginx configuration validation failed"
    NGINX_RELOADED=1
    nginx -s reload || fail "nginx reload failed; previous configuration restored"
    printf '%s\n' 'RKSS_EXTERNAL_HTTPS=--external-https' >"$ENV_FILE.new.$$"
else
    printf '%s\n' 'RKSS_EXTERNAL_HTTPS=' >"$ENV_FILE.new.$$"
fi
ENV_TOUCHED=1
install -m 600 -o "$RUN_USER" -g "$RUN_GROUP" "$ENV_FILE.new.$$" "$ENV_FILE"
rm -f "$ENV_FILE.new.$$"

USER_SED=$(escape_sed "$RUN_USER")
GROUP_SED=$(escape_sed "$RUN_GROUP")
HOME_SED=$(escape_sed "$RUN_HOME")
CONF_SED=$(escape_sed "$CONF")
sed -e "s|@RKSS_USER@|$USER_SED|g" \
    -e "s|@RKSS_GROUP@|$GROUP_SED|g" \
    -e "s|@RKSS_HOME@|$HOME_SED|g" \
    -e "s|@RKSS_CONF_DIR@|$CONF_SED|g" \
    "$SRC/deploy/rkss-portal.service" >"$RENDERED_UNIT"
[ "$(grep -c '@RKSS_' "$RENDERED_UNIT" || true)" -eq 0 ] || fail "systemd unit rendering failed"
UNIT_TOUCHED=1
install -m 644 "$RENDERED_UNIT" "$UNIT" || fail "cannot install systemd unit"
SYSTEMD_RELOADED=1
systemctl daemon-reload || fail "systemd daemon-reload failed; previous configuration restored"
ENABLE_TOUCHED=1
systemctl enable rkss-portal.service || fail "cannot enable service; previous configuration restored"
atomic_link "$NEW_RELEASE" "$CURRENT" || fail "cannot switch current release; previous configuration restored"
CURRENT_SWITCHED=1
SERVICE_RESTART_ATTEMPTED=1
systemctl restart rkss-portal.service || fail "service restart failed; previous release and unit restored"

if [ -n "$OLD_CURRENT" ]; then atomic_link "$OLD_CURRENT" "$LAST_GOOD"; else atomic_link "$NEW_RELEASE" "$LAST_GOOD"; fi
# Old direct-copy modules are unused after the atomic current switch. Removing
# them prevents deleted Python modules surviving an upgrade.
rm -rf "$APP_ROOT/host" "$APP_ROOT/portal" "$APP_ROOT/static"
COMMITTED=1
rm -f "$UNIT_BACKUP" "$ENV_BACKUP" "$NGINX_BACKUP"

echo "rkss-portal installed at $CURRENT"
echo "service account: $RUN_USER:$RUN_GROUP; configuration: $CONF"
if [ "$TLS" -eq 1 ]; then echo "open https://$SERVER_NAME (certificates were validated, not issued)"; else echo "loopback only: open http://127.0.0.1:8080 locally or through an SSH tunnel"; fi
echo "admin token: $TOKEN_FILE (0600; value not printed)"
