#!/bin/bash
# ============================================================================
# MeshCore to MQTT - Uninstaller
# ============================================================================
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
print_header() {
    echo -e "\n${BLUE}═══════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════════════${NC}\n"
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

read_prompt() {
    local prompt_text="$1"
    local response_variable="$2"

    # curl | sudo bash uses stdin for the script, so answers belong to the
    # controlling terminal. Headless invocations can supply separate stdin.
    if { : </dev/tty; } 2>/dev/null; then
        IFS= read -r -p "$prompt_text" "$response_variable" </dev/tty
        return $?
    fi
    IFS= read -r -p "$prompt_text" "$response_variable"
}

prompt_yes_no() {
    local prompt="$1"
    local default="${2:-n}"
    local response

    if [ "$default" = "y" ]; then
        prompt="$prompt [Y/n]: "
    else
        prompt="$prompt [y/N]: "
    fi

    if ! read_prompt "$prompt" response; then
        print_warning "No answer received; keeping the requested files or service." >&2
        return 1
    fi
    response=${response:-$default}

    case "$response" in
        [yY][eE][sS]|[yY]) return 0 ;;
        *) return 1 ;;
    esac
}

prompt_input() {
    local prompt="$1"
    local default="$2"
    local response

    if [ -n "$default" ]; then
        prompt="$prompt [$default]: "
    else
        prompt="$prompt: "
    fi
    if ! read_prompt "$prompt" response; then
        print_error "No answer received; uninstallation cancelled." >&2
        return 1
    fi
    echo "${response:-$default}"
}

# Default paths
for selector in MCTOMQTT_INSTALL_DIR MCTOMQTT_CONFIG_DIR; do
    selected_dir="${!selector:-}"
    if [[ -n "$selected_dir" && "$selected_dir" != /* ]]; then
        echo "Error: $selector must be an absolute path." >&2
        exit 1
    fi
done
DEFAULT_APP_DIR="${MCTOMQTT_INSTALL_DIR:-/opt/mctomqtt}"
DEFAULT_CONFIG_DIR="${MCTOMQTT_CONFIG_DIR:-/etc/mctomqtt}"
CONFIG_PHYSICAL_DIR=""
if [ -d "$DEFAULT_CONFIG_DIR" ]; then
    if ! CONFIG_PHYSICAL_DIR=$(cd -- "$DEFAULT_CONFIG_DIR" 2>/dev/null && pwd -P); then
        print_error "Cannot access configuration directory; run the uninstaller with sudo." >&2
        exit 1
    fi
    if [ "$CONFIG_PHYSICAL_DIR" = "/" ]; then
        print_error "Configuration directory must not resolve to filesystem root." >&2
        exit 1
    fi
    if [ -d "$DEFAULT_CONFIG_DIR/config.d" ] && [ ! -x "$DEFAULT_CONFIG_DIR/config.d" ]; then
        print_error "Cannot access private configuration drop-ins; run the uninstaller with sudo." >&2
        exit 1
    fi
fi
SYSTEMD_UNIT="/etc/systemd/system/mctomqtt.service"
LAUNCHD_PLIST="/Library/LaunchDaemons/com.meshcore.mctomqtt.plist"

# Detect system type
docker_container_exists() {
    [ "$(docker inspect --type container --format '{{.Name}}' mctomqtt 2>/dev/null)" = "/mctomqtt" ]
}

detect_system_type() {
    # Check for Docker container first
    if docker_container_exists; then
        echo "docker"
    elif command -v systemctl &> /dev/null; then
        echo "systemd"
    elif [ "$(uname)" = "Darwin" ]; then
        echo "launchd"
    else
        echo "unknown"
    fi
}

# Detect the service user from the installed systemd unit
detect_service_user() {
    if [ -f "$SYSTEMD_UNIT" ]; then
        grep -E '^User=' "$SYSTEMD_UNIT" 2>/dev/null | cut -d'=' -f2
    fi
}

# Remove systemd service
remove_systemd_service() {
    if [ -f "$SYSTEMD_UNIT" ]; then
        print_info "Stopping and removing systemd service (requires sudo)..."

        if sudo systemctl is-active --quiet mctomqtt.service; then
            sudo systemctl stop mctomqtt.service
            print_success "Service stopped"
        fi

        if sudo systemctl is-enabled --quiet mctomqtt.service; then
            sudo systemctl disable mctomqtt.service
            print_success "Service disabled"
        fi

        sudo rm -f "$SYSTEMD_UNIT"
        sudo systemctl daemon-reload
        print_success "Service removed"
    else
        print_info "No systemd service found"
    fi
}

# Remove launchd service (system-level daemon)
remove_launchd_service() {
    if [ -f "$LAUNCHD_PLIST" ]; then
        print_info "Stopping and removing launchd service (requires sudo)..."

        if launchctl list | grep -q com.meshcore.mctomqtt; then
            sudo launchctl unload "$LAUNCHD_PLIST" 2>/dev/null || true
            print_success "Service unloaded"
        fi

        sudo rm -f "$LAUNCHD_PLIST"
        print_success "Service removed"

        # Clean up log files
        if prompt_yes_no "Remove log files?" "y"; then
            sudo rm -f /var/log/mctomqtt.log
            sudo rm -f /var/log/mctomqtt-error.log
            print_success "Log files removed"
        fi
    else
        print_info "No launchd service found"
    fi
}

# Remove Docker container and image
remove_docker() {
    if docker_container_exists; then
        print_info "Stopping and removing Docker container..."

        # Stop if running
        if [ "$(docker inspect --type container --format '{{.State.Running}}' mctomqtt 2>/dev/null)" = "true" ]; then
            docker stop mctomqtt
            print_success "Container stopped"
        fi

        # Remove container
        docker rm mctomqtt
        print_success "Container removed"
    else
        print_info "No Docker container found"
    fi

    # Ask about removing image
    if docker image inspect mctomqtt:latest >/dev/null 2>&1; then
        if prompt_yes_no "Remove Docker image (mctomqtt:latest)?" "y"; then
            docker rmi mctomqtt:latest
            print_success "Docker image removed"
        fi
    fi
}

# Remove service user
remove_service_user() {
    local svc_user="$1"

    if [ -z "$svc_user" ]; then
        return
    fi

    # Don't offer to remove root or the current user
    if [ "$svc_user" = "root" ] || [ "$svc_user" = "$(whoami)" ]; then
        return
    fi

    # Check if the user actually exists
    if ! id "$svc_user" &>/dev/null; then
        return
    fi

    print_warning "Service was running as user: $svc_user"
    if prompt_yes_no "Remove service user '$svc_user'?" "n"; then
        if sudo userdel "$svc_user" 2>/dev/null; then
            print_success "User '$svc_user' removed"
        else
            print_error "Failed to remove user '$svc_user' - you may need to remove it manually"
        fi
    else
        print_info "Keeping user '$svc_user'"
    fi
}

# Remove configuration files
backup_user_config() {
    local user_toml="$1"
    local backup_file
    backup_file=$(mktemp "$HOME/mctomqtt-user-toml-backup-$(date +%Y%m%d-%H%M%S).toml.XXXXXXXX") || return 1
    # mktemp creates a new mode-600 file without replacing an existing path.
    # The shell writes its own private file while sudo reads private config.
    if ! sudo cat -- "$user_toml" > "$backup_file"; then
        rm -f -- "$backup_file"
        print_error "Configuration backup failed; keeping configuration." >&2
        return 1
    fi
    print_success "Configuration backed up to: $backup_file"
}

remove_config() {
    local config_dir="$DEFAULT_CONFIG_DIR"
    local user_toml="$config_dir/config.d/99-user.toml"
    if [ ! -f "$user_toml" ] && [ -f "$config_dir/config.d/00-user.toml" ]; then
        user_toml="$config_dir/config.d/00-user.toml"
    fi

    if [ ! -d "$config_dir" ]; then
        print_info "No configuration directory found at $config_dir"
        return
    fi

    # Offer to back up the user TOML before removal
    if [ -f "$user_toml" ]; then
        print_info "User configuration file: $user_toml"

        if prompt_yes_no "Do you want to back up $(basename "$user_toml") before uninstalling?" "y"; then
            if ! backup_user_config "$user_toml"; then
                KEEP_CONFIG=true
                return 1
            fi
        fi
    fi

    # Remove config files and directories
    if prompt_yes_no "Remove configuration directory ($config_dir)?" "y"; then
        if [ -f "$config_dir/config.toml" ]; then
            sudo rm -f "$config_dir/config.toml"
            print_success "Removed $config_dir/config.toml"
        fi

        if [ -d "$config_dir/config.d" ]; then
            sudo rm -rf "$config_dir/config.d"
            print_success "Removed $config_dir/config.d/"
        fi

        sudo rm -rf "$config_dir"
        print_success "Removed $config_dir/"
        KEEP_CONFIG=false
    else
        print_info "Keeping configuration directory: $config_dir"
        KEEP_CONFIG=true
    fi
}

# Main uninstallation
main() {
    print_header "MeshCore to MQTT Uninstaller"

    echo "This will remove MeshCore to MQTT from your system."
    echo ""

    # Determine application directory
    APP_DIR=$(prompt_input "Application directory" "$DEFAULT_APP_DIR")
    APP_DIR="${APP_DIR/#\~/$HOME}"  # Expand tilde

    if [[ "$APP_DIR" != /* ]]; then
        print_error "Application directory must be an absolute path: $APP_DIR"
        exit 1
    fi
    if [ ! -d "$APP_DIR" ]; then
        print_error "Application directory not found: $APP_DIR"
        exit 1
    fi
    if ! APP_PHYSICAL_DIR=$(cd -- "$APP_DIR" && pwd -P); then
        print_error "Cannot access application directory: $APP_DIR"
        exit 1
    fi
    if [ "$APP_PHYSICAL_DIR" = "/" ] || [ ! -f "$APP_DIR/mctomqtt.py" ]; then
        print_error "Application directory must contain an installed mctomqtt.py: $APP_DIR"
        exit 1
    fi

    print_warning "This will remove:"
    echo "  Application files: $APP_DIR"
    echo "  Configuration:     $DEFAULT_CONFIG_DIR"
    echo ""

    if ! prompt_yes_no "Are you sure you want to continue?" "n"; then
        print_info "Uninstallation cancelled"
        exit 0
    fi

    # Detect service user from systemd unit before removing the service
    SVC_USER=""
    if [ -f "$SYSTEMD_UNIT" ]; then
        SVC_USER=$(detect_service_user)
        if [ -n "$SVC_USER" ]; then
            print_info "Detected service user: $SVC_USER"
        fi
    fi

    # Stop and remove service
    print_header "Removing Service"

    SYSTEM_TYPE=$(detect_system_type)
    print_info "Detected system type: $SYSTEM_TYPE"

    case "$SYSTEM_TYPE" in
        docker)
            remove_docker
            ;;
        systemd)
            remove_systemd_service
            ;;
        launchd)
            remove_launchd_service
            ;;
        *)
            print_info "Unknown system type - skipping service removal"
            ;;
    esac

    # Handle configuration files
    print_header "Configuration Files"

    KEEP_CONFIG=false
    remove_config

    # Remove application directory
    print_header "Removing Files"

    KEEP_APP=false
    if [ "$KEEP_CONFIG" = true ] &&
        [[ "$CONFIG_PHYSICAL_DIR" = "$APP_PHYSICAL_DIR" || "$CONFIG_PHYSICAL_DIR" = "$APP_PHYSICAL_DIR/"* ]]; then
        KEEP_APP=true
        print_warning "Keeping application directory because it contains the retained configuration: $APP_DIR"
    else
        print_info "Removing application directory..."
        sudo rm -rf -- "$APP_DIR"
        print_success "Application directory removed: $APP_DIR"
    fi

    # Offer to remove the service user (systemd only)
    if [ "$SYSTEM_TYPE" = "systemd" ] && [ -n "$SVC_USER" ]; then
        print_header "Service User"
        remove_service_user "$SVC_USER"
    fi

    # Final message
    print_header "Uninstallation Finished"

    if [ "$KEEP_APP" = true ]; then
        echo "Service removal finished. Application and configuration directories kept."
        echo "Application directory: $APP_DIR"
        echo "Configuration directory: $DEFAULT_CONFIG_DIR"
    elif [ "$KEEP_CONFIG" = true ]; then
        echo "MeshCore to MQTT has been removed (configuration kept)."
        echo "Configuration directory: $DEFAULT_CONFIG_DIR"
    else
        echo "MeshCore to MQTT has been completely removed."
    fi

    echo ""
    print_success "Uninstaller finished!"
}

# Run main
main "$@"
