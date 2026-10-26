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
LOG_FILE= CURRENT_STEP= STEP=0 TOTAL_STEPS=11
UI=1 ERROR_UI=2 COLOR= RESET=
HTTP_HOST=0.0.0.0 HTTP_PORT=8080
SPI_DEVICE=/dev/spidev0.0 SPI_CLOCK_HZ=25000000

die() {
    printf 'Error: %s\n' "$*" >&"$ERROR_UI"
    if [[ -n $LOG_FILE ]]; then
        printf 'Error: %s\n' "$*" >>"$LOG_FILE"
        printf 'Installation log: %s\n' "$LOG_FILE" >&"$ERROR_UI"
    fi
    exit 1
}
say() {
    printf '%s\n' "$*" >&"$UI"
    if [[ -n $LOG_FILE ]]; then printf '%s\n' "$*" >>"$LOG_FILE"; fi
}
support() { (cd -- "$SOURCE_DIR"; python3 -m tools.install_support "$@"); }

init_ui() {
    exec {UI}>&1 {ERROR_UI}>&2
    COLOR= RESET=
    if [[ -t 1 && -z ${NO_COLOR+x} && ${TERM:-dumb} != dumb ]]; then
        COLOR=$'\033[1;36m'; RESET=$'\033[0m'
    fi
}

section() {
    printf '\n%s----------------------------------------\n%s\n----------------------------------------%s\n\n' \
        "$COLOR" "$1" "$RESET" >&"$UI"
}

init_log() {
    # mktemp creates an exclusive 0600 file, including under a hostile /var/tmp.
    LOG_FILE=$(mktemp /var/tmp/fmheadend-install.XXXXXXXX.log)
    chmod 0600 "$LOG_FILE"
    say "Installation log: $LOG_FILE"
    printf 'FMheadend installation: %s\nPlugin revision: %s\n' "$(date -u)" "$GST_PLUGINS_REVISION" >>"$LOG_FILE"
}

failure() {
    local status=$1 line=$2
    trap - ERR
    # ERR is inherited by support() subshells and command substitutions. Let
    # their failures propagate; only the parent installer reports the error.
    if ((BASH_SUBSHELL > 0)); then exit "$status"; fi
    printf '\nInstallation failed%s (line %s, exit %s).\n' \
        "${CURRENT_STEP:+ during $CURRENT_STEP}" "$line" "$status" >&"$ERROR_UI"
    if [[ -n $LOG_FILE ]]; then
        printf '\nInstallation failed: step=%s line=%s exit=%s\n' \
            "${CURRENT_STEP:-configuration}" "$line" "$status" >>"$LOG_FILE"
        printf 'Recent command output:\n' >&"$ERROR_UI"
        tail -n 12 -- "$LOG_FILE" >&"$ERROR_UI"
        printf 'Full installation log: %s\n' "$LOG_FILE" >&"$ERROR_UI"
    fi
    printf 'Services may remain stopped. No automatic rollback was performed.\n' >&"$ERROR_UI"
    printf 'Service diagnostics: journalctl -u fmheadendd -u fmheadend-muxd -n 50\n' >&"$ERROR_UI"
    exit "$status"
}

run_step() {
    local label=$1
    shift
    CURRENT_STEP=$label
    ((STEP+=1))
    printf '[%s/%s] %s...\n' "$STEP" "$TOTAL_STEPS" "$label" >&"$UI"
    printf '\n[%s/%s] %s\n' "$STEP" "$TOTAL_STEPS" "$label" >>"$LOG_FILE"
    # Do not put this invocation in an if/||: that disables errexit inside
    # functions and could allow a failed command to be followed by mutations.
    "$@" >>"$LOG_FILE" 2>&1
    say '      OK'
    CURRENT_STEP=
}

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
    local prompt=$1 default=$2 low=$3 high=$4 name=$5
    while true; do
        printf '%s (default: %s - %s): ' "$prompt" "$default" "$name" >&"$TTY"
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
    # Headings and prompts stay together even when stdout/stderr are redirected.
    # Color eligibility was decided from the original stdout in init_ui.
    UI=$TTY ERROR_UI=$TTY
    init_log
    WORK=$(mktemp -d /tmp/fmheadend-install.XXXXXX)
    local plan
    plan=$(support hardware "$MODEL_FILE" "$SPI_DEVICE" "$SPI_CLOCK_HZ")
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
        HTTP_HOST=${RUNTIME[4]}; HTTP_PORT=${RUNTIME[5]}
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
    section 'System Detection'
    say "Detected platform: $MODEL"
    case $MODE in
        fresh) INSTALLATION_LABEL='Fresh installation' ;;
        upgrade) INSTALLATION_LABEL='Upgrade' ;;
        partial) INSTALLATION_LABEL='Partial installation (existing files detected)' ;;
    esac
    say "Installation: $INSTALLATION_LABEL"
    if [[ $MODE == partial ]]; then
        say 'Existing installation files were detected, but no database was found.'
        say "Expected database: $DATABASE"
        say 'Existing files will be preserved; a new database needs your approval.'
        confirm 'Initialize a NEW database for this incomplete first installation?' no || die 'Stopped; restore the database if this was an upgrade.'
    fi
    if [[ $MODE != upgrade ]]; then
        if [[ $HAVE_ENV == no ]]; then
            section 'FM Region'
            say 'Select the FM frequency plan for your region.'
            say ''
            support regions >&"$UI"
            choose_number Region 1 0 3 Europe
            REGION=$REPLY
        else
            say "Preserving existing region $REGION and backend arguments."
        fi
        section 'Default Audio Profile'
        say 'Basic presets use lightweight or partial audio processing.'
        say 'They are suitable for lower-powered Raspberry Pi models.'
        say ''
        say 'Light and Moderate presets provide a more complete FM audio'
        say 'processing chain, but require more CPU power, especially when'
        say 'running multiple transmitters.'
        say ''
        say 'Not sure what to choose? You can change the default Audio Profile'
        say 'anytime in the FMheadend WebUI.'
        say ''
        local catalog identifier name index=0 default=0 default_name=
        catalog=$(support profiles "$SOURCE_DIR/audio-profiles/default")
        PROFILE_IDS=()
        PROFILE_NAMES=()
        while IFS=$'\t' read -r identifier name; do
            PROFILE_IDS+=("$identifier"); ((index+=1))
            PROFILE_NAMES+=("$name")
            if [[ $identifier == "$RECOMMENDED_PROFILE" ]]; then
                default=$index; default_name=$name
            fi
            printf '  %s) %s\n' "$index" "$name" >&"$UI"
            case $identifier in
                default/basic_limiter.json) say '     Default for Pi Zero 2 W (provisional)' ;;
                default/basic_wideband.json) say '     Default for Pi 2-3 (64-bit models)' ;;
                default/moderate_loud.json) say '     Default for Pi 4-5' ;;
            esac
        done <<< "$catalog"
        ((default > 0)) || die 'Platform default Audio Profile is missing from the menu.'
        say ''
        choose_number 'Default Audio Profile' "$default" 1 "$index" "$default_name"
        DEFAULT_PROFILE=${PROFILE_IDS[REPLY-1]}
        DEFAULT_PROFILE_NAME=${PROFILE_NAMES[REPLY-1]}
        section 'WebUI Authentication'
        say 'HTTP Basic Authentication protects access to the FMheadend WebUI.'
        say ''
        if [[ $HAVE_ENV == no ]]; then
            if confirm 'Enable authentication?' no; then
                AUTH=yes
                if [[ -e $PASSWD_FILE ]]; then
                    say 'Existing WebUI credentials will be preserved.'
                else
                    say ''
                    say 'Credential entry follows the system dependencies and account setup,'
                    say 'before native builds. This provides the required bcrypt dependency.'
                fi
            fi
        else
            AUTH=preserve
            say 'Existing authentication settings and credentials will be preserved.'
        fi
        section 'Raspberry Pi Hardware'
        if [[ $HAVE_ENV == no ]]; then
            say 'Detected platform:'
            say "  $MODEL"
            say ''
            say 'Proposed configuration:'
            say ''
            printf '%s\n' "${HARDWARE_PLAN[@]:5}" >&"$UI"
            say ''
            say 'These settings may require a reboot.'
            say ''
            if confirm 'Configure Raspberry Pi hardware automatically?' yes; then
                HARDWARE=yes
            else
                MANUAL_PENDING=yes
            fi
        else
            say 'Existing startup configuration preserved; hardware setup is left unchanged.'
        fi
    fi
    section 'Installation Summary'
    local region_name authentication hardware_label
    local -a region_names=(Expert Europe 'United States' Japan)
    region_name=${region_names[REGION]}
    authentication=Disabled
    [[ $AUTH != yes && -z ${EXISTING_PASSWD:-} ]] || authentication=Enabled
    hardware_label='Preserved'
    if [[ $MODE != upgrade && $HAVE_ENV == no ]]; then
        hardware_label=Manual
        [[ $HARDWARE != yes ]] || hardware_label=Automatic
    fi
    printf '%-19s %s\n' 'Platform:' "$MODEL" 'Installation:' "$INSTALLATION_LABEL" \
        'Region:' "$region_name" 'Default profile:' "${DEFAULT_PROFILE_NAME:-Preserved}" \
        'Authentication:' "$authentication" 'Hardware setup:' "$hardware_label" \
        'WebUI port:' "$HTTP_PORT" 'HTTP bind:' "$HTTP_HOST" >&"$UI"
    say ''
    if [[ $MODE == upgrade ]]; then
        say 'Preserving database, environment files, credentials, custom profiles'
        say 'and boot settings.'
    fi
    if [[ $MODE == partial ]]; then
        say 'A new database will be initialized. Existing configuration, credentials,'
        say 'and custom profiles are preserved. Boot changes follow your hardware choice.'
    fi
    if [[ -n $LOG_FILE ]]; then
        printf '\nConfiguration: mode=%s model=%s region=%s profile=%s auth=%s hardware=%s\nDatabase: %s\nWebUI: %s/webui\nI2C: %s\nHTTP: %s:%s\n' \
            "$MODE" "$MODEL" "$REGION" "${DEFAULT_PROFILE:-preserved}" "$AUTH" "$HARDWARE" \
            "$DATABASE" "$SHARE_DIR" "$I2C_DEVICE" "$HTTP_HOST" "$HTTP_PORT" >>"$LOG_FILE"
    fi
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
    apt-get install -y git ca-certificates build-essential cmake pkg-config python3-dev python3-venv python3-bcrypt \
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

install_backend() {
    python3 -m venv --system-site-packages "$VENV"
    "$VENV/bin/python" -m pip install --upgrade "$SOURCE_DIR/daemon"
}

install_mux() {
    cmake -S "$SOURCE_DIR/muxd" -B "$WORK/mux-build" -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF
    cmake --build "$WORK/mux-build" --parallel 1
    cmake --install "$WORK/mux-build"
}

install_plugins() {
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
        section 'WebUI Authentication - Credentials'
        say 'Please enter a username and password for the FMheadend WebUI.'
        say ''
        # System bcrypt was installed with the prerequisites. Neither prompts
        # nor credential utility output pass through the installation log.
        python3 "$SOURCE_DIR/tools/create_passwd.py" --file "$PASSWD_FILE" --quiet >&"$UI"
        chown root:fmheadend "$PASSWD_FILE"
        chmod 0640 "$PASSWD_FILE"
        say ''
        say 'WebUI credentials configured successfully.'
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
        (set -o noclobber; printf 'MUXD_ARGS="--transport spidev --spi-device %s --spi-clock-hz %s --runtime-dir /run/fmheadend"\n' "$SPI_DEVICE" "$SPI_CLOCK_HZ" > "$CONFIG_DIR/muxd.env")
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
            section 'Raspberry Pi Hardware - Existing Settings'
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
    if [[ $REBOOT_REQUIRED != yes && $MANUAL_PENDING != yes ]]; then
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
        local pid diagnostic
        pid=$(systemctl show --property MainPID --value fmheadend-muxd.service)
        if diagnostic=$(support realtime "$pid" 2>&1); then
            say "$diagnostic"
        else
            say "WARNING: Could not verify mux realtime scheduling: $diagnostic"
            say 'Expected SCHED_FIFO / 50. Inspect systemctl status fmheadend-muxd.'
        fi
    fi
}

complete_installation() {
    section 'FMheadend installation completed!'
    say 'Installed systemd services:'
    say ''
    say '  fmheadendd.service'
    say '    Backend, API and WebUI'
    say ''
    say '  fmheadend-muxd.service'
    say '    SPI audio transport'
    say ''
    if [[ $REBOOT_REQUIRED == yes ]]; then
        say 'Services: Enabled (will start after reboot)'
        say ''
        say 'WebUI access (available after reboot):'
    elif [[ $MANUAL_PENDING == yes ]]; then
        say 'Services: Enabled, stopped pending manual hardware preparation'
        say 'Prepare I2C/SPI, then start both services. Reboot if necessary.'
        say ''
        say 'WebUI access (available after hardware preparation and service startup):'
    else
        say 'Services: Enabled and running; backend HTTP is ready'
        say ''
        say 'WebUI access (services running; subject to your network/firewall rules):'
    fi
    local settings urls
    settings=$(support runtime "$CONFIG_DIR/fmheadendd.env" "$DATABASE")
    mapfile -t RUNTIME <<< "$settings"
    if urls=$(support urls "${RUNTIME[4]}" "${RUNTIME[5]}" 2>&1); then
        say "$urls"
    else
        printf '%s\n' "$urls" >>"$LOG_FILE"
        say '  Network addresses could not be inspected. Check your HTTP bind and IPs.'
    fi
    say ''
    say 'Useful commands:'
    say ''
    say '  systemctl status fmheadendd fmheadend-muxd'
    say ''
    say '  journalctl -fu fmheadendd -u fmheadend-muxd'
    say ''
    say '  sudo systemctl restart fmheadendd fmheadend-muxd'
    say ''
    say 'Check the WebUI System page for Controller availability.'
    if [[ -n ${RUNTIME[3]} ]]; then
        say 'Use HTTPS or an encrypted tunnel for authentication on untrusted networks.'
    fi
    say "Installation log: $LOG_FILE"
}

offer_reboot() {
    [[ $REBOOT_REQUIRED == yes ]] || return 0
    say ''
    say 'A reboot is required to activate the new I2C/SPI configuration.'
    say ''
    if confirm 'Reboot now?' no; then
        say 'Rebooting now...'
        if ! systemctl reboot; then
            say 'WARNING: Installation succeeded, but the reboot request failed.'
            say 'Reboot when ready: sudo systemctl reboot'
        fi
    else
        say 'Reboot when ready: sudo systemctl reboot'
    fi
}

configure_application() {
    write_environment
    initialize_database
    configure_hardware
}

cleanup() {
    if [[ $WORK =~ ^/tmp/fmheadend-install\.[A-Za-z0-9]+$ && -d $WORK && ! -L $WORK ]]; then rm -rf -- "$WORK"; fi
}

main() {
    init_ui
    parse_arguments "$@"
    trap cleanup EXIT
    trap 'failure "$?" "$LINENO"' ERR
    trap 'printf "\nInstallation cancelled. No reboot was requested.\n" >&"$ERROR_UI"; exit 130' INT
    preflight
    detect_installation
    collect_configuration
    section 'Installing FMheadend'
    say 'Builds can take several minutes. Command output is saved to the log.'
    run_step 'Backing up existing installation' backup_existing
    run_step 'Installing system dependencies' install_dependencies
    run_step 'Preparing system account' install_account
    CURRENT_STEP='Configuring WebUI credentials'
    configure_password
    CURRENT_STEP=
    # Credentials are collected before downtime and all native builds.
    run_step 'Stopping services for update' stop_services
    run_step 'Installing Python backend' install_backend
    run_step 'Building SPI multiplexer' install_mux
    run_step 'Installing FM DSP plugins' install_plugins
    run_step 'Installing WebUI' install_webui
    run_step 'Applying configuration' configure_application
    run_step 'Configuring services' install_services
    run_step 'Verifying installation' verify_installation
    complete_installation
    offer_reboot
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
