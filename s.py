import sys
import os
import time
import threading
import tkinter as tk
import keyboard
import mss
import vgamepad as vg
import ctypes
from PIL import Image
from typing import Literal, List
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------
# CONFIGURATION & CONTROLS
# ---------------------------------------------------------
HOTKEY = 'f9'
MODEL_ID = "gemini-3.5-flash-lite"

# Initialize Virtual Xbox 360 Controller with retry logic
GAMEPAD = None
for attempt in range(3):
    try:
        GAMEPAD = vg.VX360Gamepad()
        print(f"✅ [SYSTEM] Virtual gamepad initialized (Attempt {attempt + 1}).")
        break
    except Exception as e:
        print(f"⚠️ [WARNING] Gamepad init failed (Attempt {attempt + 1}/3): {e}")
        time.sleep(1.5)  # Give ViGEmBus a moment to wake up

if not GAMEPAD:
    print("❌ [ERROR] Could not connect to ViGEmBus after multiple attempts. Exiting.")
    sys.exit(1)

ai_active = False
stuck_counter = 0
last_action_signature = None

# ---------------------------------------------------------
# EXTERNAL GAME STATE DOCUMENT
# ---------------------------------------------------------
class GameState(BaseModel):
    character: str = "Defect (or STS2 equivalent)"
    floor: int = 1
    act: int = 1
    hp_status: str = "Full"
    gold: int = 99
    current_deck: List[str] = Field(
        default_factory=lambda: ["Strike x4", "Defend x4", "Zap", "Dualcast"]
    )
    relics: List[str] = Field(
        default_factory=lambda: ["Cracked Core"]
    )
    current_strategy: str = (
        "Establish early damage, prioritize playing Zap and Dualcast "
        "when an orb is loaded. Path aggressively towards Act 1 Elites."
    )
    action_history: List[str] = Field(default_factory=list)

    def add_action(self, action_summary: str):
        self.action_history.append(action_summary)
        if len(self.action_history) > 8:
            self.action_history.pop(0)

    def get_summary_prompt(self) -> str:
        history_text = "\n".join(f"- {a}" for a in self.action_history) if self.action_history else "No previous actions recorded."
        return f"""
==================== CURRENT KNOWLEDGE ====================
Act {self.act} | Floor {self.floor} | HP: {self.hp_status} | Gold: {self.gold}
Character: {self.character}
Relics: {', '.join(self.relics)}
Deck: {', '.join(self.current_deck)}
Macro Strategy: {self.current_strategy}

RECENT ACTION HISTORY (Rolling Memory):
{history_text}

CRITICAL: Read the actual screen for your current HP, Energy, and drawn cards. 
The knowledge above is baseline, but the screen is the absolute truth.
============================================================
"""

GAME_STATE = GameState()

# ---------------------------------------------------------
# STRUCTURED AGENT SCHEMA (UNIFIED)
# ---------------------------------------------------------
class UnifiedAgentResponse(BaseModel):
    screen_type: Literal["combat", "map", "reward", "event", "unknown"]
    reasoning: str
    action_description: str
    action: Literal["dpad_up", "dpad_down", "dpad_left", "dpad_right", "confirm", "cancel", "end_turn", "wait"]

# ---------------------------------------------------------
# FLOATING HUD OVERLAY
# ---------------------------------------------------------
class OverlayHUD:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Slay the Spire 2 Multi-Agent Copilot")
        self.root.geometry("450x220+20+20")
        self.root.overrideredirect(True)
        self.root.wm_attributes("-topmost", True)
        self.root.attributes("-alpha", 0.92)
        self.root.configure(bg='#121212')

        self.top_frame = tk.Frame(self.root, bg="#121212")
        self.top_frame.pack(fill="x", padx=10, pady=(8, 2))

        self.lbl_status = tk.Label(
            self.top_frame, text="● STANDBY [Press F9]", fg="#FFCC00", bg="#121212", 
            font=("Consolas", 11, "bold"), anchor="w"
        )
        self.lbl_status.pack(side="left")

        self.btn_close = tk.Button(
            self.top_frame, text=" X ", bg="#FF3330", fg="white", bd=0, 
            font=("Consolas", 10, "bold"), command=self.close_app, cursor="hand2"
        )
        self.btn_close.pack(side="right")

        self.lbl_agent = tk.Label(
            self.root, text="Active Agent: Idle", fg="#00E5FF", bg="#121212",
            font=("Consolas", 10, "bold"), anchor="w"
        )
        self.lbl_agent.pack(fill="x", padx=10, pady=1)

        self.lbl_action = tk.Label(
            self.root, text="Action: Ready", fg="#00FFCC", bg="#121212", 
            font=("Consolas", 10, "bold"), anchor="w"
        )
        self.lbl_action.pack(fill="x", padx=10, pady=1)

        self.lbl_thought = tk.Label(
            self.root, text="Waiting for autopilot activation...", fg="#E0E0E0", bg="#181818", 
            font=("Consolas", 9), wraplength=430, justify="left", anchor="nw", relief="solid", bd=1
        )
        self.lbl_thought.pack(fill="both", expand=True, padx=10, pady=(2, 6))

        self.bot_frame = tk.Frame(self.root, bg="#121212")
        self.bot_frame.pack(fill="x", padx=10, pady=(0, 8))
        
        self.btn_pause = tk.Button(
            self.bot_frame, text="RESUME AUTOPILOT (F9)", bg="#006400", fg="white", 
            font=("Consolas", 10, "bold"), bd=0, command=lambda: toggle_ai(), cursor="hand2"
        )
        self.btn_pause.pack(fill="x")

    def close_app(self):
        print("\n[SYSTEM] Shutting down Copilot...")
        os._exit(0) 

    def update_hud(self, status_text, agent_text, action_text, thought_text, status_color="#00FF00"):
        self.root.after(0, self._apply_update, status_text, agent_text, action_text, thought_text, status_color)

    def _apply_update(self, status, agent, action, thought, color):
        self.lbl_status.config(text=status, fg=color)
        self.lbl_agent.config(text=f"Active Agent: {agent}")
        self.lbl_action.config(text=action)
        self.lbl_thought.config(text=thought)
        
        if ai_active:
            self.btn_pause.config(text="PAUSE AUTOPILOT (F9)", bg="#8B0000")
        else:
            self.btn_pause.config(text="RESUME AUTOPILOT (F9)", bg="#006400")

hud = None

# ---------------------------------------------------------
# HELPER UTILITIES
# ---------------------------------------------------------
def toggle_ai():
    global ai_active
    ai_active = not ai_active
    
    # Send mouse cursor entirely off-screen to avoid UI interference
    if ai_active:
        try:
            ctypes.windll.user32.SetCursorPos(9999, 9999) 
        except Exception as e:
            print(f"⚠️ [WARNING] Could not park mouse cursor: {e}")
            
    status_str = "● AUTOPILOT ACTIVE" if ai_active else "● STANDBY [Press F9]"
    color = "#00FF00" if ai_active else "#FFCC00"
    print(f"\n⚡ [SYSTEM] AUTOPILOT {'ENGAGED' if ai_active else 'DISENGAGED'}.")
    
    if hud:
        hud.update_hud(status_str, "Idle", "Action: Idle", "Awaiting screenshot uplink...", color)

def capture_screen():
    with mss.MSS() as sct:
        monitor = sct.monitors[1]  
        sct_img = sct.grab(monitor)
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        # Reduced from 1280x720 to 1024x576 to optimize payload size and API upload speed
        img.thumbnail((1024, 576), Image.Resampling.LANCZOS)
        return img

# ---------------------------------------------------------
# UNIFIED AI AGENT
# ---------------------------------------------------------
def run_unified_agent(client: genai.Client, game_img: Image.Image, state_summary: str, stuck_warning: str) -> UnifiedAgentResponse:
    system_instruction = """
    You are playing Slay the Spire 2 using an XBOX CONTROLLER. Look for the glowing UI highlight/reticle to know your current cursor position.
    
    STEP 1: Classify the screen (combat, map, reward, event, unknown).
    STEP 2: Determine the next single controller input.

    RULES:
    1. Navigation: Output 'dpad_up', 'dpad_down', 'dpad_left', or 'dpad_right' to move the highlight.
    2. Selection: Output 'confirm' to select the currently highlighted option.
    3. COMBAT: To play a card: D-pad to the card -> 'confirm' -> D-pad to target -> 'confirm'. If energy is 0, output 'end_turn'.
    4. MAP: ONLY pick paths connected by a dotted line.
    5. REWARDS/SCREENS: You CANNOT use the D-pad to navigate to the "Proceed" or "Skip" buttons in the bottom right. If you are ready to move on, you MUST output 'end_turn' (this triggers the Y button shortcut).
    6. MENUS: If stuck in a full-screen menu by mistake, output 'cancel'.
    """
    prompt = f"{state_summary}\n{stuck_warning}\nDetermine the current screen type and the next controller input."
    
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        response_mime_type="application/json",
        response_schema=UnifiedAgentResponse,
        temperature=0.0
    )
    resp = client.models.generate_content(
        model=MODEL_ID,
        contents=[prompt, game_img],
        config=config
    )
    return resp.parsed

# ---------------------------------------------------------
# MAIN CONTROL LOOP
# ---------------------------------------------------------
def ai_loop():
    global ai_active, stuck_counter, last_action_signature, hud, GAME_STATE
    
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("❌ [ERROR] GEMINI_API_KEY environment variable not found.")
        return

    client = genai.Client(
        api_key=api_key,
        # Increased timeout to 60_000 (60 seconds)
        http_options=types.HttpOptions(timeout=60_000)
    )
    
    # Virtual Gamepad Button Mapping
    btn_map = {
        "dpad_up": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
        "dpad_down": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
        "dpad_left": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
        "dpad_right": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
        "confirm": vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
        "cancel": vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
        "end_turn": vg.XUSB_BUTTON.XUSB_GAMEPAD_Y
    }

    while True:
        if not ai_active:
            time.sleep(0.05)
            continue

        try:
            start_time = time.time()
            if hud:
                hud.update_hud("● AUTOPILOT ACTIVE", "Unified Agent", "Analyzing screen...", "Capturing game frame...", "#00FF00")

            # Capture raw image, no grid overlay
            raw_image = capture_screen()
            
            stuck_warning = ""
            if stuck_counter >= 3:
                stuck_warning = f"⚠️ WARNING: You have executed '{last_action_signature}' {stuck_counter} times and state has not changed. You are stuck. Output 'cancel' (B button) or choose a different action."

            state_summary = GAME_STATE.get_summary_prompt()

            # Single API Call
            res = run_unified_agent(client, raw_image, state_summary, stuck_warning)
            screen_type = res.screen_type.lower()
            act = res.action.lower()
            reasoning = res.reasoning
            act_desc = res.action_description

            latency = round(time.time() - start_time, 2)
            GAME_STATE.add_action(f"[{screen_type.upper()}] Action: {act.upper()} - {act_desc}")

            # Smart Stuck Detection (Ignores D-Pad movement)
            current_sig = f"{screen_type}_{act}"
            directional_inputs = ["dpad_up", "dpad_down", "dpad_left", "dpad_right"]
            
            if current_sig == last_action_signature and act not in directional_inputs and act != "wait":
                stuck_counter += 1
            else:
                stuck_counter = 0
            
            last_action_signature = current_sig

            # Controller-native recovery (Spams B button to back out of menus)
            if stuck_counter >= 5:
                print("🚨 [RECOVERY]: Loop detected! Pressing B (Cancel) to reset UI state.")
                for _ in range(2):
                    GAMEPAD.press_button(button=btn_map["cancel"])
                    GAMEPAD.update()
                    time.sleep(0.1)
                    GAMEPAD.release_button(button=btn_map["cancel"])
                    GAMEPAD.update()
                    time.sleep(0.2)
                stuck_counter = 0

            status_display = "● AUTOPILOT ACTIVE" if stuck_counter < 3 else f"● STUCK DETECTED ({stuck_counter}x)"
            status_col = "#00FF00" if stuck_counter < 3 else "#FF3330"
            act_display = f"Action: {act.upper()} [{latency}s]"
            agent_display = f"{screen_type.upper()} Agent"

            if hud:
                hud.update_hud(status_display, agent_display, act_display, reasoning, status_col)

            print("\n" + "="*60)
            print(f"🤖  Screen State: {screen_type.upper()}")
            print(f"⏱️   Latency:      {latency}s | Stuck Counter: {stuck_counter}")
            print(f"🧠  Reasoning:    {reasoning}")
            print(f"🕹️   Action:       {act.upper()} | {act_desc}")
            print("="*60 + "\n")

            # ---------------------------------------------------------
            # VIRTUAL CONTROLLER EXECUTION & DYNAMIC SLEEPS
            # ---------------------------------------------------------
            if act in btn_map:
                target_btn = btn_map[act]
                GAMEPAD.press_button(button=target_btn)
                GAMEPAD.update()
                time.sleep(0.15)
                GAMEPAD.release_button(button=target_btn)
                GAMEPAD.update()
                
                # Fast sleep for D-pad scrolling, longer sleep for animations
                if act in directional_inputs:
                    time.sleep(0.3)
                else:
                    time.sleep(1.5)  
                    
            elif act == "wait":
                time.sleep(1.0)

        except Exception as e:
            err_msg = str(e)
            if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                print("⚠️  [RATE LIMIT] API Quota Exceeded (429). Pausing 10s...")
                if hud:
                    hud.update_hud("● RATE LIMITED", "System", "Cooling down...", "Quota exceeded. Waiting 10s...", "#FFCC00")
                time.sleep(10.0)
            elif "504" in err_msg or "DEADLINE_EXCEEDED" in err_msg:
                print("⏳ [WARNING] API 504 Timeout! Retrying step with backoff...")
                if hud:
                    hud.update_hud("● TIMEOUT", "System", "API Deadline Exceeded", "Retrying request in 3 seconds...", "#FFCC00")
                time.sleep(3.0)
            elif "timeout" in err_msg.lower():
                print("⏳ [WARNING] Network Timeout! Retrying step...")
                time.sleep(1.0)
            else:
                print(f"⚠️  [ERROR] Execution Exception: {e}")
                if hud:
                    hud.update_hud("● ERROR", "System", "Execution Exception", err_msg, "#FF0000")
                time.sleep(1.5)

# ---------------------------------------------------------
# SCRIPT ENTRYPOINT
# ---------------------------------------------------------
if __name__ == "__main__":
    print(f"""
    ==================================================
    🤖 Slay the Spire 2 Unified AI Copilot
    ==================================================
    Architecture: Controller Only (No Mouse Interference)
    Status      : STANDBY
    Toggle      : Press [{HOTKEY.upper()}] to engage/disengage AI.
    ==================================================
    """)
    keyboard.add_hotkey(HOTKEY, toggle_ai)
    
    hud = OverlayHUD()
    
    ai_thread = threading.Thread(target=ai_loop, daemon=True)
    ai_thread.start()
    
    hud.root.mainloop()