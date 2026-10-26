#!/usr/bin/env bash
# Interactive source installer for Raspberry Pi OS Lite (64-bit).
set -Eeuo pipefail
# Software must be service-readable; credentials and backups use explicit modes.
umask 022

SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# Deliberate release dependency; never replace with a moving branch/tag.
GST_PLUGINS_REVISION=30bd239b33b31885615d7b92a7bf664d3d1ad851
GST_PLUGINS_URL=https://github.com/csalotzky/fmheadend-gst-plugins.git
VENV=/opt/fmheadend/venv
DATA_DIR=/var/lib/fmheadend
CONFIG_DIR=/etc/fmheadend
SHARE_DIR=/usr/share/fmheadend
UNIT_DIR=/etc/systemd/system
MUX_BINARY=/usr/local/bin/fmheadend-muxd
MODEL_FILE=/proc/device-tree/model
BACKUP_DIR=/var/backups/fmheadend
DATABASE=$DATA_DIR/fmheadend.db
PASSWD_FILE=$DATA_DIR/htpasswd
WORK= MODE= REGION=1 DEFAULT_PROFILE= AUTH=no HARDWARE=no
REBOOT_REQUIRED=no MANUAL_PENDING=no ARCHIVE= TTY=

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }
support() { (cd -- "$SOURCE_DIR"; python3 -m tools.install_support "$@"); }

confirm() {
    local prompt=$1 default=${2:-no} answer
    while true; do
        printf '%s [%s]: ' "$prompt" "$([[ $default == yes ]] && printf Y/n || printf y/N)" >&"$TTY"
        IFS= read -r -u "$TTY" answer || die 'Terminal input ended; installation cancelled.'
        case ${answer:-$default} in
            y|Y|yes|YES) return 0 ;;
            n|N|no|NO) return 1 ;;
            *) printf 'Enter y or n.\n' >&"$TTY" ;;
        esac
    done
}

choose_number() {
    local prompt=$1 default=$2 low=$3 high=$4
    while true; do
        printf '%s [%s]: ' "$prompt" "$default" >&"$TTY"
        IFS= read -r -u "$TTY" REPLY || die 'Terminal input ended; installation cancelled.'
        REPLY=${REPLY:-$default}
        if [[ $REPLY =~ ^[0-9]+$ && ${#REPLY} -le 3 ]] && ((10#$REPLY >= low && 10#$REPLY <= high)); then
            REPLY=$((10#$REPLY))
            return
        fi
        printf 'Choose %s to %s.\n' "$low" "$high" >&"$TTY"
    done
}

parse_arguments() {
    while (($#)); do
        case $1 in
            --webui-archive) (($# >= 2)) || die '--webui-archive requires a path'; ARCHIVE=$2; shift 2 ;;
            -h|--help) say 'Usage: sudo ./install.sh --webui-archive /path/to/fmheadend-webui.tar.gz'; exit 0 ;;
            *) die "Unknown option: $1" ;;
        esac
    done
    [[ -n $ARCHIVE && -f $ARCHIVE ]] || die 'Supply an existing --webui-archive .tar.gz file.'
    ARCHIVE=$(cd -- "$(dirname -- "$ARCHIVE")" && printf '%s/%s' "$PWD" "$(basename -- "$ARCHIVE")")
}

preflight() {
    ((EUID == 0)) || die 'Run this installer with sudo.'
    [[ $(uname -s) == Linux && $(uname -m) == aarch64 ]] || die 'Require Raspberry Pi OS Lite, 64-bit (aarch64).'
    # This root-owned OS file is trusted; service environment files are never sourced.
    . /etc/os-release
    [[ ${ID:-} == debian || ${ID:-} == raspbian ]] || die 'Require Raspberry Pi OS Lite (Debian-based).'
    for command in python3 apt-get systemctl systemd-analyze runuser getent install; do
        command -v "$command" >/dev/null || die "Missing prerequisite: $command"
    done
    python3 -c 'import struct, sys; assert sys.version_info >= (3, 11) and struct.calcsize("P") == 8, "64-bit Python 3.11+ required"'
    for path in daemon/pyproject.toml muxd/CMakeLists.txt tools/setup_pi.py tools/create_passwd.py systemd/fmheadendd.service systemd/fmheadend-muxd.service; do
        [[ -f $SOURCE_DIR/$path ]] || die "Incomplete source checkout: $path"
    done
    exec {TTY}<>/dev/tty || die 'Interactive installation requires a controlling terminal.'
    WORK=$(mktemp -d /tmp/fmheadend-install.XXXXXX)
    local plan
    plan=$(support hardware "$MODEL_FILE")
    mapfile -t HARDWARE_PLAN <<< "$plan"
    MODEL=${HARDWARE_PLAN[0]}
    I2C_DEVICE=${HARDWARE_PLAN[1]}
    RECOMMENDED_PROFILE=${HARDWARE_PLAN[2]}
    support webui "$ARCHIVE" "$WORK/webui"
}

detect_installation() {
    local settings
    HAVE_ENV=no
    if [[ -e $CONFIG_DIR/fmheadendd.env ]]; then
        HAVE_ENV=yes
        settings=$(support runtime "$CONFIG_DIR/fmheadendd.env" "$DATABASE")
        mapfile -t RUNTIME <<< "$settings"
        DATABASE=${RUNTIME[0]}; REGION=${RUNTIME[1]}; I2C_DEVICE=${RUNTIME[2]}
        EXISTING_PASSWD=${RUNTIME[3]}
        if [[ -n $EXISTING_PASSWD && ! -f $EXISTING_PASSWD ]]; then
            die "Configured password file is missing: $EXISTING_PASSWD; restore it before installation."
        fi
    fi
    if [[ -e $DATABASE || -L $DATABASE ]]; then
        support check-db "$DATABASE"
        [[ $HAVE_ENV == yes ]] || die 'Database exists but backend environment is missing; restore it before upgrading.'
        MODE=upgrade
        [[ -f $CONFIG_DIR/muxd.env ]] || die 'Existing installation has no muxd.env; restore it before upgrading.'
    elif [[ $HAVE_ENV == yes || -e $VENV || -e $DATA_DIR || -e $MUX_BINARY || -e $CONFIG_DIR/muxd.env || -e $UNIT_DIR/fmheadendd.service ]]; then
        MODE=partial
    else
        MODE=fresh
    fi
    local unit
    for unit in fmheadendd.service fmheadend-muxd.service; do
        if [[ -f $UNIT_DIR/$unit ]]; then support service-contract "$UNIT_DIR/$unit" "$SOURCE_DIR/systemd/$unit"; fi
    done
}

collect_configuration() {
    say "Platform: $MODEL; installation mode: $MODE"
    if [[ $MODE == partial ]]; then
        say "No database exists at $DATABASE. Existing files will be preserved."
        confirm 'Initialize a NEW database for this incomplete first installation?' no || die 'Stopped; restore the database if this was an upgrade.'
    fi
    if [[ $MODE != upgrade ]]; then
        if [[ $HAVE_ENV == no ]]; then
            printf 'Region: 0 Expert, 1 Europe, 2 United States, 3 Japan\n' >&"$TTY"
            choose_number Region 1 0 3
            REGION=$REPLY
        else
            say "Preserving existing region $REGION and backend arguments."
        fi
        local catalog identifier name index=0 default=1
        catalog=$(support profiles "$SOURCE_DIR/audio-profiles/default")
        PROFILE_IDS=()
        while IFS=$'\t' read -r identifier name; do
            PROFILE_IDS+=("$identifier"); ((index+=1))
            if [[ $identifier == "$RECOMMENDED_PROFILE" ]]; then
                default=$index; name="$name (recommended)"
            fi
            printf '%s) %s\n' "$index" "$name" >&"$TTY"
        done <<< "$catalog"
        choose_number 'Default Audio Profile' "$default" 1 "$index"
        DEFAULT_PROFILE=${PROFILE_IDS[REPLY-1]}
        if [[ $HAVE_ENV == no ]]; then
            if confirm 'Enable HTTP Basic Authentication?' no; then AUTH=yes; fi
        else
            AUTH=preserve
        fi
        if [[ $HAVE_ENV == no ]]; then
            say "${HARDWARE_PLAN[3]}; ${HARDWARE_PLAN[4]}"
            say 'SPI0 /dev/spidev0.0; 25 MHz; SPI buffer 8192 bytes; i2c-dev on boot.'
            if confirm 'Let setup_pi.py configure the Raspberry Pi hardware?' yes; then
                HARDWARE=yes
            else
                MANUAL_PENDING=yes
            fi
        else
            say 'Existing startup configuration preserved; hardware setup is left unchanged.'
        fi
    fi
    say "Database: $DATABASE"
    if [[ $MODE == upgrade ]]; then
        say 'Preserving database, environment files, credentials, custom profiles and boot settings.'
    else
        say "Region: $REGION; default Audio Profile: $DEFAULT_PROFILE; authentication: $AUTH"
        say "Backend I2C device: $I2C_DEVICE (Controller address default 0x62); hardware setup: $HARDWARE"
    fi
    say 'HTTP defaults: port 8080, all interfaces for a new service configuration.'
    say 'Use HTTPS or a trusted encrypted transport for credentials across untrusted networks.'
    say "WebUI: $SHARE_DIR/webui; GStreamer plugins: $GST_PLUGINS_REVISION"
    confirm 'Proceed with installation?' no || die 'Installation cancelled.'
}

backup_existing() {
    [[ $MODE != fresh ]] || return 0
    install -d -m 0700 "$BACKUP_DIR"
    local backup file
    backup=$(mktemp -d "$BACKUP_DIR/$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")
    if [[ -f $DATABASE ]]; then support backup-db "$DATABASE" "$backup/fmheadend.db"; fi
    install -d -m 0700 "$backup/environment" "$backup/units"
    for file in "$CONFIG_DIR"/*.env; do [[ ! -e $file ]] || cp -p -- "$file" "$backup/environment/"; done
    for file in "$UNIT_DIR"/fmheadend*.service; do [[ ! -e $file ]] || cp -p -- "$file" "$backup/units/"; done
    for file in "$UNIT_DIR"/fmheadend*.service.d; do [[ ! -e $file ]] || cp -a -- "$file" "$backup/units/"; done
    if [[ -f $PASSWD_FILE ]]; then cp -p -- "$PASSWD_FILE" "$backup/htpasswd"; fi
    if [[ -n ${EXISTING_PASSWD:-} && $EXISTING_PASSWD != "$PASSWD_FILE" ]]; then
        cp -p -- "$EXISTING_PASSWD" "$backup/configured-htpasswd"
    fi
    say "Pre-installation backup: $backup"
}

stop_services() {
    local unit
    for unit in fmheadendd.service fmheadend-muxd.service; do
        if systemctl cat "$unit" >/dev/null 2>&1; then systemctl stop "$unit"; fi
    done
}

install_dependencies() {
    apt-get update
    apt-get install -y git ca-certificates build-essential cmake pkg-config python3-dev python3-venv \
        libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
        python3-gi python3-gst-1.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
        gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
        gstreamer1.0-alsa alsa-utils i2c-tools lsp-plugins-lv2
}

install_account() {
    groupadd --system --force fmheadend
    if ! id fmheadend >/dev/null 2>&1; then
        useradd --system --gid fmheadend --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin fmheadend
    fi
    local group
    for group in i2c spi audio; do getent group "$group" >/dev/null || die "Missing device group: $group"; done
    usermod -aG i2c,spi,audio fmheadend
    install -d -o fmheadend -g fmheadend -m 0750 "$DATA_DIR" "$DATA_DIR/audio-profiles/custom"
    install -d -m 0755 "$(dirname "$VENV")" "$CONFIG_DIR" "$SHARE_DIR" "$SHARE_DIR/audio-profiles/default"
}

install_software() {
    python3 -m venv --system-site-packages "$VENV"
    "$VENV/bin/python" -m pip install --upgrade "$SOURCE_DIR/daemon"
    cmake -S "$SOURCE_DIR/muxd" -B "$WORK/mux-build" -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF
    cmake --build "$WORK/mux-build" --parallel 1
    cmake --install "$WORK/mux-build"
    git init -q "$WORK/plugins"
    git -C "$WORK/plugins" remote add origin "$GST_PLUGINS_URL"
    git -C "$WORK/plugins" fetch --depth 1 origin "$GST_PLUGINS_REVISION"
    git -C "$WORK/plugins" checkout --detach FETCH_HEAD
    [[ $(git -C "$WORK/plugins" rev-parse HEAD) == "$GST_PLUGINS_REVISION" ]] || die 'Plugin revision mismatch.'
    make -C "$WORK/plugins" -j1 plugin
    make -C "$WORK/plugins" install
    install -m 0644 "$SOURCE_DIR"/audio-profiles/default/*.json "$SHARE_DIR/audio-profiles/default/"
    "$VENV/bin/python" -c 'import gi; gi.require_version("Gst", "1.0"); from gi.repository import Gst; Gst.init(None)'
    runuser -u fmheadend -- gst-inspect-1.0 fmheadendlimiter >/dev/null
    runuser -u fmheadend -- gst-inspect-1.0 fmheadendclipper >/dev/null
}

install_webui() {
    local staged previous
    [[ ! -L $SHARE_DIR/webui ]] || die 'WebUI destination is a symlink; review it manually.'
    staged=$(mktemp -d "$SHARE_DIR/.webui-new.XXXXXX")
    cp -a -- "$WORK/webui/." "$staged/"
    chmod 0755 "$staged"
    if [[ -e $SHARE_DIR/webui ]]; then
        previous=$(mktemp -d "$SHARE_DIR/.webui-old.XXXXXX")
        mv -- "$SHARE_DIR/webui" "$previous/webui"
    fi
    mv -- "$staged" "$SHARE_DIR/webui"
    # Only the old static tree, after complete replacement; never application data.
    if [[ -n ${previous:-} ]]; then
        [[ $previous == "$SHARE_DIR"/.webui-old.* && ! -L $previous ]] || die 'Unsafe old WebUI cleanup path.'
        rm -rf -- "$previous"
    fi
}

configure_password() {
    [[ $AUTH == yes && $MODE != upgrade ]] || return 0
    if [[ ! -e $PASSWD_FILE ]]; then
        "$VENV/bin/python" "$SOURCE_DIR/tools/create_passwd.py" --file "$PASSWD_FILE"
        chown root:fmheadend "$PASSWD_FILE"
        chmod 0640 "$PASSWD_FILE"
    else
        say "Preserving existing password file: $PASSWD_FILE"
    fi
}

write_environment() {
    # Noclobber also protects files created between preflight and this step.
    if [[ ! -e $CONFIG_DIR/fmheadendd.env ]]; then
        local authentication=
        if [[ $AUTH == yes ]]; then authentication=" --passwd-file $PASSWD_FILE"; fi
        (set -o noclobber; printf 'FMHEADEND_ARGS="--host 0.0.0.0 --port 8080 --i2c-device %s --region %s --webui-dir %s/webui%s"\n' \
            "$I2C_DEVICE" "$REGION" "$SHARE_DIR" "$authentication" > "$CONFIG_DIR/fmheadendd.env")
        chmod 0644 "$CONFIG_DIR/fmheadendd.env"
    fi
    if [[ ! -e $CONFIG_DIR/muxd.env ]]; then
        (set -o noclobber; printf 'MUXD_ARGS="--transport spidev --spi-device /dev/spidev0.0 --spi-clock-hz 25000000 --runtime-dir /run/fmheadend"\n' > "$CONFIG_DIR/muxd.env")
        chmod 0644 "$CONFIG_DIR/muxd.env"
    fi
}

initialize_database() {
    if [[ $MODE == upgrade ]]; then
        [[ -f $DATABASE ]] || die 'Existing database disappeared; refusing to create another.'
    else
        runuser -u fmheadend -- "$VENV/bin/fmheadend-init-db" --database "$DATABASE" --region "$REGION" \
            --default-audio-profile "$DEFAULT_PROFILE" --audio-profiles-default-dir "$SHARE_DIR/audio-profiles/default"
    fi
}

configure_hardware() {
    [[ $MODE != upgrade && $HARDWARE == yes ]] || return 0
    local output
    if output=$(python3 "$SOURCE_DIR/tools/setup_pi.py" 2>&1); then
        say "$output"
    else
        say "$output"
        if [[ $output == *'--overwrite-existing'* ]]; then
            say 'setup_pi.py found existing relevant settings; no files were changed.'
            if confirm 'Permit setup_pi.py to replace those settings (with backups)?' no; then
                output=$(python3 "$SOURCE_DIR/tools/setup_pi.py" --overwrite-existing)
                say "$output"
            else
                say 'Hardware configuration skipped; configure it manually before starting services.'
                MANUAL_PENDING=yes
                return
            fi
        else
            die 'Hardware configuration failed; review the setup_pi.py diagnostic above.'
        fi
    fi
    [[ $output == *'Reboot required: yes'* ]] && REBOOT_REQUIRED=yes
    return 0
}

install_services() {
    install -m 0644 "$SOURCE_DIR/systemd/fmheadend-muxd.service" "$SOURCE_DIR/systemd/fmheadendd.service" "$UNIT_DIR/"
    systemd-analyze verify "$UNIT_DIR/fmheadend-muxd.service" "$UNIT_DIR/fmheadendd.service"
    systemctl daemon-reload
    systemctl enable fmheadend-muxd.service fmheadendd.service
    if [[ $MODE != upgrade && ( ! -e $I2C_DEVICE || ! -e /dev/spidev0.0 ) ]]; then MANUAL_PENDING=yes; fi
    if [[ $REBOOT_REQUIRED == yes || $MANUAL_PENDING == yes ]]; then
        say 'Software installed; services enabled but left stopped pending reboot/hardware preparation.'
        return
    fi
    systemctl restart fmheadend-muxd.service fmheadendd.service
}

verify_installation() {
    [[ -s $SHARE_DIR/webui/index.html && -x $VENV/bin/fmheadendd && -x $MUX_BINARY ]] || die 'Required installed files are missing.'
    if [[ $REBOOT_REQUIRED == yes ]]; then
        say 'Installation complete. Reboot required; run sudo reboot when ready. No reboot was performed.'
    elif [[ $MANUAL_PENDING == yes ]]; then
        say 'Software installation complete. Prepare I2C/SPI, reboot if necessary, then start both services manually.'
    else
        systemctl is-active --quiet fmheadend-muxd.service
        systemctl is-active --quiet fmheadendd.service
        local settings attempt ready=no
        settings=$(support runtime "$CONFIG_DIR/fmheadendd.env" "$DATABASE")
        mapfile -t RUNTIME <<< "$settings"
        for ((attempt=0; attempt<20; attempt++)); do
            if support health "${RUNTIME[4]}" "${RUNTIME[5]}" 2>/dev/null; then ready=yes; break; fi
            sleep 1
        done
        [[ $ready == yes ]] || die 'Backend HTTP did not become ready; inspect journalctl -u fmheadendd.service.'
        say 'Installation complete; services running. Controller connectivity is not an installation prerequisite.'
        say 'Check systemctl status and the WebUI System page for current Controller availability.'
    fi
}

cleanup() {
    if [[ $WORK =~ ^/tmp/fmheadend-install\.[A-Za-z0-9]+$ && -d $WORK && ! -L $WORK ]]; then rm -rf -- "$WORK"; fi
}

main() {
    parse_arguments "$@"
    trap cleanup EXIT
    trap 'printf "Installation failed near line %s. Services may remain stopped; no automatic rollback was performed.\n" "$LINENO" >&2' ERR
    preflight
    detect_installation
    collect_configuration
    backup_existing
    stop_services
    install_dependencies
    install_account
    install_software
    install_webui
    configure_password
    write_environment
    initialize_database
    configure_hardware
    install_services
    verify_installation
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
