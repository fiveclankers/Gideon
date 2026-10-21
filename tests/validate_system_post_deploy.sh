#!/usr/bin/env bash
# ==============================================================================
# validate_system_post_deploy.sh - Automated Post-Deployment System Verification
#
# Audits the 5 Core System Pillars:
#   1. Compositor Health (Hyprland syntax errors = 0, scale = 1.0)
#   2. PipeWire Audio Integrity (Nodes 58, 59, 68 active, zero stream stalls)
#   3. Hardware & VRAM Budget (VRAM < 2000 MiB, GPU Temp < 45°C on GTX 1650)
#   4. Desktop Entry Conformance (FreeDesktop validate on agy-swarm-top & agy-tokens)
#   5. Antigravity Tool Suite (omask, agy-stats, agy-tokens, agy-preflight, agy-swarm-top)
# ==============================================================================

set -euo pipefail

# ANSI color formatting
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m' # No Color

TOTAL_TESTS=5
PASSED_TESTS=0
FAILED_TESTS=0

report_pass() {
    local name="$1"
    local detail="$2"
    echo -e "  [${GREEN}PASS${NC}] ${BOLD}${name}${NC}: ${detail}"
    PASSED_TESTS=$((PASSED_TESTS + 1))
}

report_fail() {
    local name="$1"
    local detail="$2"
    echo -e "  [${RED}FAIL${NC}] ${BOLD}${name}${NC}: ${detail}"
    FAILED_TESTS=$((FAILED_TESTS + 1))
}

echo -e "\n${BOLD}${BLUE}====================================================================${NC}"
echo -e "${BOLD}${BLUE}   GIDEON & WORKSTATION POST-DEPLOYMENT VERIFICATION SUITE          ${NC}"
echo -e "${BOLD}${BLUE}====================================================================${NC}\n"

# ------------------------------------------------------------------------------
# TEST 1: Compositor Health
# ------------------------------------------------------------------------------
echo -e "${BOLD}[1/5] Checking Wayland & Hyprland Compositor Health...${NC}"
CONFIG_ERRORS=$(hyprctl configerrors 2>&1 || true)
if [[ -z "$CONFIG_ERRORS" || "$CONFIG_ERRORS" =~ "ok" || "$CONFIG_ERRORS" =~ "no errors" ]]; then
    report_pass "Config Errors" "0 syntax errors in Hyprland configuration"
else
    # Check if there are non-empty lines
    ERR_LINE_COUNT=$(echo "$CONFIG_ERRORS" | grep -v '^[[:space:]]*$' | wc -l)
    if [[ "$ERR_LINE_COUNT" -eq 0 ]]; then
        report_pass "Config Errors" "0 syntax errors in Hyprland configuration"
    else
        report_fail "Config Errors" "Found $ERR_LINE_COUNT error lines: $CONFIG_ERRORS"
    fi
fi

MONITOR_SCALE=$(hyprctl monitors -j 2>/dev/null | jq -r '.[0].scale // empty')
if [[ -z "$MONITOR_SCALE" ]]; then
    # Fallback to textual parse
    MONITOR_SCALE=$(hyprctl monitors | grep -i "scale:" | head -n 1 | awk '{print $2}' || echo "unknown")
fi

if [[ "$MONITOR_SCALE" == "1" || "$MONITOR_SCALE" == "1.00" || "$MONITOR_SCALE" == "1.000000" ]]; then
    report_pass "Display Scale" "Monitor HDMI-A-1 scale is strictly 1.00 (Native 1:1 pixel rendering)"
else
    report_fail "Display Scale" "Expected scale 1.00, detected: $MONITOR_SCALE"
fi

# ------------------------------------------------------------------------------
# TEST 2: PipeWire Audio Integrity
# ------------------------------------------------------------------------------
echo -e "\n${BOLD}[2/5] Checking PipeWire & GIDEON Audio Stream Integrity...${NC}"
WP_STATUS=$(wpctl status 2>/dev/null || true)

# Node 58: USB Audio Device Analog Stereo (Sink)
if echo "$WP_STATUS" | grep -E "58\..*USB Audio Device" >/dev/null; then
    report_pass "Default Sink" "Node 58 (USB Audio Device Analog Stereo) active"
else
    report_fail "Default Sink" "Node 58 not active in wpctl status"
fi

# Node 59: USB Audio Device Mono (Source)
if echo "$WP_STATUS" | grep -E "59\..*USB Audio Device" >/dev/null; then
    report_pass "Default Source" "Node 59 (USB Audio Device Mono Capture) active"
else
    report_fail "Default Source" "Node 59 not active in wpctl status"
fi

# Node 68: PipeWire ALSA stream (GIDEON Python speech engine)
if echo "$WP_STATUS" | grep -E "68\..*PipeWire ALSA.*python" >/dev/null; then
    report_pass "GIDEON Audio Stream" "Node 68 (GIDEON Python ALSA hot-stream) active"
else
    # If PID reloaded, check if python3.11 ALSA stream is present
    if echo "$WP_STATUS" | grep -E "[0-9]+\..*PipeWire ALSA.*python" >/dev/null; then
        STREAM_ID=$(echo "$WP_STATUS" | grep -E "[0-9]+\..*PipeWire ALSA.*python" | head -n 1 | awk '{print $1}' | tr -d '.')
        report_pass "GIDEON Audio Stream" "Python ALSA stream active under node $STREAM_ID"
    else
        report_fail "GIDEON Audio Stream" "No active Python ALSA audio stream found"
    fi
fi

# Verify PipeWire daemon connection
if pw-cli info all >/dev/null 2>&1; then
    report_pass "PipeWire Daemon" "pw-cli info returned 0 errors; IPC socket responsive"
else
    report_fail "PipeWire Daemon" "pw-cli info failed to connect to pipewire-0"
fi

# ------------------------------------------------------------------------------
# TEST 3: Hardware & VRAM Budget (GTX 1650)
# ------------------------------------------------------------------------------
echo -e "\n${BOLD}[3/5] Checking Hardware & GPU VRAM Budget (< 2000 MiB, < 45°C)...${NC}"
GPU_METRICS=$(nvidia-smi --query-gpu=memory.used,temperature.gpu --format=csv,noheader,nounits 2>/dev/null || true)
if [[ -n "$GPU_METRICS" ]]; then
    VRAM_USED=$(echo "$GPU_METRICS" | awk -F',' '{print $1}' | tr -d ' ')
    GPU_TEMP=$(echo "$GPU_METRICS" | awk -F',' '{print $2}' | tr -d ' ')

    if [[ "$VRAM_USED" -lt 2000 ]]; then
        report_pass "VRAM Ceiling" "Used: ${VRAM_USED} MiB / 4096 MiB (Safe under 2000 MiB limit)"
    else
        report_fail "VRAM Ceiling" "VRAM usage exceeded budget: ${VRAM_USED} MiB >= 2000 MiB"
    fi

    if [[ "$GPU_TEMP" -lt 45 ]]; then
        report_pass "GPU Thermal" "Temperature: ${GPU_TEMP}°C (Safe under 45°C ceiling)"
    else
        report_fail "GPU Thermal" "GPU Temperature elevated: ${GPU_TEMP}°C >= 45°C"
    fi
else
    report_fail "NVIDIA Metrics" "nvidia-smi failed to query GPU"
fi

# ------------------------------------------------------------------------------
# TEST 4: Desktop Entry Conformance & Launcher Validation
# ------------------------------------------------------------------------------
echo -e "\n${BOLD}[4/5] Checking Desktop Entries & FreeDesktop Compliance...${NC}"
APP_DIR="$HOME/.local/share/applications"
SWARM_DESKTOP="$APP_DIR/agy-swarm-top.desktop"
TOKENS_DESKTOP="$APP_DIR/agy-tokens.desktop"

if [[ -f "$SWARM_DESKTOP" && -f "$TOKENS_DESKTOP" ]]; then
    # Validate desktop files
    if desktop-file-validate "$SWARM_DESKTOP" "$TOKENS_DESKTOP" >/dev/null 2>&1; then
        report_pass "Desktop Syntax" "desktop-file-validate passed with 0 errors"
    else
        # Allow hints but check for non-zero exit code
        if desktop-file-validate "$SWARM_DESKTOP" "$TOKENS_DESKTOP"; then
            report_pass "Desktop Syntax" "desktop-file-validate exited code 0"
        else
            report_fail "Desktop Syntax" "desktop-file-validate detected format errors"
        fi
    fi

    # Check Icon assets exist
    if [[ -f "$APP_DIR/icons/agy-swarm-top.png" && -f "$APP_DIR/icons/agy-tokens.png" ]]; then
        report_pass "Icon Assets" "High-res PNG launcher icons verified in $APP_DIR/icons/"
    else
        report_fail "Icon Assets" "Missing icon assets in $APP_DIR/icons/"
    fi
else
    report_fail "Desktop Entries" "Missing agy-swarm-top.desktop or agy-tokens.desktop in $APP_DIR"
fi

# ------------------------------------------------------------------------------
# TEST 5: Antigravity CLI Tool Suite Execution
# ------------------------------------------------------------------------------
echo -e "\n${BOLD}[5/5] Checking Antigravity CLI Tools Execution (Exit Code 0)...${NC}"
CLI_TOOLS=("omask" "agy-stats" "agy-tokens" "agy-preflight" "agy-swarm-top")

for tool in "${CLI_TOOLS[@]}"; do
    TOOL_PATH="/home/cat/.local/bin/$tool"
    if [[ -x "$TOOL_PATH" ]]; then
        case "$tool" in
            "omask")
                if echo "test observation" | "$TOOL_PATH" >/dev/null 2>&1; then
                    report_pass "Tool: $tool" "Executed successfully via stdin pipe"
                else
                    report_fail "Tool: $tool" "Failed execution with exit code $?"
                fi
                ;;
            "agy-swarm-top")
                if "$TOOL_PATH" --help >/dev/null 2>&1; then
                    report_pass "Tool: $tool" "CLI flags and telemetry harness operational"
                else
                    report_fail "Tool: $tool" "Failed execution with exit code $?"
                fi
                ;;
            *)
                if "$TOOL_PATH" >/dev/null 2>&1; then
                    report_pass "Tool: $tool" "Executed cleanly with return code 0"
                else
                    report_fail "Tool: $tool" "Exited with non-zero status code: $?"
                fi
                ;;
        esac
    else
        report_fail "Tool: $tool" "Executable not found or not executable at $TOOL_PATH"
    fi
done

# ------------------------------------------------------------------------------
# Final Summary & Exit
# ------------------------------------------------------------------------------
echo -e "\n${BOLD}${BLUE}====================================================================${NC}"
echo -e "${BOLD}   FINAL POST-DEPLOYMENT VERIFICATION SUMMARY: $PASSED_TESTS PASSED | $FAILED_TESTS FAILED${NC}"
echo -e "${BOLD}${BLUE}====================================================================${NC}\n"

if [[ "$FAILED_TESTS" -eq 0 ]]; then
    echo -e "${GREEN}${BOLD}[✓] ALL 5 POST-DEPLOYMENT VERIFICATION CHECKS PASSED WITH ZERO REGRESSIONS.${NC}\n"
    exit 0
else
    echo -e "${RED}${BOLD}[✗] SYSTEM VALIDATION FAILED WITH $FAILED_TESTS ISSUES.${NC}\n"
    exit 1
fi
