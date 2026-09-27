import sys
import ctypes

# ---------------------------------------------------------
# DPI AWARENESS FIX
# Must be executed before importing PyAutoGUI
# ---------------------------------------------------------
if sys.platform == 'win32':
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()

import os
import time
import threading
import tkinter as tk
import keyboard
import pyautogui
import mss
from PIL import Image
from typing import Literal
from pydantic import BaseModel
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------
# CONFIGURATION & STATE
# ---------------------------------------------------------
HOTKEY = 'f9'
pyautogui.FAILSAFE = True 
pyautogui.PAUSE = 0.05  

ai_active = False
action_history = []
stuck_counter = 0
last_action_signature = None

RUN_STATE = {
    "character": "Defect (or STS2 equivalent)",
    "floor": "1",
    "act": "1",
    "hp_status": "Full",
    "current_deck": "Strike x4, Defend x4, Zap, Dualcast",
    "relics": "Cracked Core",
    "current_strategy": "Establish early damage, prioritize playing Zap and dualcast when an orb is loaded."
}

# ---------------------------------------------------------
# FLOATING HUD OVERLAY
# ---------------------------------------------------------
class OverlayHUD:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Slay the Spire AI Copilot")
        self.root.geometry("420x200+20+20")
        self.root.overrideredirect(True)
        self.root.wm_attributes("-topmost", True)
        self.root.attributes("-alpha", 0.90)
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

        self.lbl_action = tk.Label(
            self.root, text="Action: Ready", fg="#00FFCC", bg="#121212", 
            font=("Consolas", 10, "bold"), anchor="w"
        )
        self.lbl_action.pack(fill="x", padx=10, pady=2)

        self.lbl_thought = tk.Label(
            self.root, text="Waiting for autopilot activation...", fg="#E0E0E0", bg="#181818", 
            font=("Consolas", 9), wraplength=400, justify="left", anchor="nw", relief="solid", bd=1
        )
        self.lbl_thought.pack(fill="both", expand=True, padx=10, pady=(2, 6))

        self.bot_frame = tk.Frame(self.root, bg="#121212")
        self.bot_frame.pack(fill="x", padx=10, pady=(0, 8))
        
        self.btn_pause = tk.Button(
            self.bot_frame, text="RESUME AUTOPILOT (F9)", bg="#006400", fg="white", 
            font=("Consolas", 10, "bold"), bd=0, command=toggle_ai, cursor="hand2"
        )
        self.btn_pause.pack(fill="x")

    def close_app(self):
        print("\n[SYSTEM] Shutting down Copilot...")
        os._exit(0) 

    def update_hud(self, status_text, action_text, thought_text, status_color="#00FF00"):
        self.root.after(0, self._apply_update, status_text, action_text, thought_text, status_color)

    def _apply_update(self, status, action, thought, color):
        self.lbl_status.config(text=status, fg=color)
        self.lbl_action.config(text=action)
        self.lbl_thought.config(text=thought)
        
        if ai_active:
            self.btn_pause.config(text="PAUSE AUTOPILOT (F9)", bg="#8B0000")
        else:
            self.btn_pause.config(text="RESUME AUTOPILOT (F9)", bg="#006400")

hud = None

class SpireAction(BaseModel):
    intended_meta_strategy: str 
    action_description: str 
    reasoning: str
    action: Literal["click", "play_card", "wait", "end_turn"]
    target_y_norm: int  
    target_x_norm: int  
    release_y_norm: int = 0
    release_x_norm: int = 0

def toggle_ai():
    global ai_active
    ai_active = not ai_active
    status_str = "● AUTOPILOT ACTIVE" if ai_active else "● STANDBY [Press F9]"
    color = "#00FF00" if ai_active else "#FFCC00"
    print(f"\n⚡ [SYSTEM] AUTOPILOT {'ENGAGED' if ai_active else 'DISENGAGED'}.")
    
    if hud:
        hud.update_hud(status_str, "Action: Idle", "Awaiting screenshot uplink...", color)

def capture_screen():
    with mss.mss() as sct:
        monitor = sct.monitors[1]  
        sct_img = sct.grab(monitor)
        
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        img.thumbnail((1280, 720), Image.Resampling.LANCZOS)
        
        return img, monitor['width'], monitor['height']

def park_mouse():
    pyautogui.moveTo(10, 10)

def ai_loop():
    global ai_active, action_history, stuck_counter, last_action_signature, hud, RUN_STATE
    
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("❌ [ERROR] GEMINI_API_KEY not found.")
        return

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=12_000)
    )
    model_id = "gemini-3.5-flash-lite" 
    
    # MODIFIED SYSTEM INSTRUCTIONS
    system_instruction = """
    You are a top-tier Slay the Spire AI speedrunner. 
    Analyze the screenshot, the run state text, and the recent action history to execute optimal, high-level plays.
    
    CRITICAL VISION & COMBAT RULES:
    - ENERGY CHECK: You MUST read the red energy meter in the bottom left corner before acting. If it says "0/3" or your current energy is 0, you CANNOT play cards.
    - CARD STATE: If the cards in your hand are grayed out, they are unplayable. Do not attempt to play them.
    - Always check enemy HP and intent. Block if they are attacking for high damage.
    
    ACTION TYPES:
    1. play_card: Select a card from hand (target_x, target_y) and apply it to an enemy or center screen (release_x, release_y).
    2. click: Select a map node, reward, choice, or UI button.
    3. end_turn: Use this IMMEDIATELY IF you have 0 energy OR no playable cards left.
    4. wait: Use if combat animations are actively playing.
    
    COORDINATE GUIDELINES (0-1000 Normalized Scale):
    - Cards in hand: Bottom center (y ~ 880-960). Aim for the center of the card artwork.
    - Enemies: Right-center (x ~ 650-850, y ~ 500-650). Aim for DEAD CENTER of the visual body.
    - Self-target/Skills/Buffs: Aim upper-middle (x=500, y=300).
    - Map Navigation: The true clickable hitbox for map nodes is strictly lower than their visual center. You MUST aim for the VERY BOTTOM-CENTER base of the node icon. Do not aim for the top or middle of the artwork. Do NOT click the dotted paths.
    """

    gen_config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        response_mime_type="application/json",
        response_schema=SpireAction,
        temperature=0.0
    )
    
    while True:
        if not ai_active:
            time.sleep(0.05)
            continue

        try:
            start_time = time.time()
            if hud:
                hud.update_hud("● AUTOPILOT ACTIVE", "Analyzing screen...", "Uplinking vision payload to Gemini...", "#00FF00")

            image, screen_w, screen_h = capture_screen()
            
            stuck_warning = ""
            if stuck_counter >= 2:
                stuck_warning = f"\n⚠️ CRITICAL WARNING: You have tried the exact same action {stuck_counter} times in a row, but the game screen IS NOT CHANGING. DO NOT repeat the same move! Choose a different card, aim at a different area, or select 'end_turn'."

            # Assemble rolling text memory of the last 6 actions to preserve state
            history_text = "\n".join(action_history[-6:]) if action_history else "No previous actions yet."

            dynamic_prompt = f"""
            CURRENT RUN STATE:
            Deck: {RUN_STATE['current_deck']}
            Relics: {RUN_STATE['relics']}
            HP Status: {RUN_STATE['hp_status']}
            Overarching Strategy: {RUN_STATE['current_strategy']}
            
            RECENT ACTION HISTORY (Use this context to remember what you just did):
            {history_text}
            
            {stuck_warning}
            """
            
            # Use generate_content instead of chats to prevent image stacking
            response = client.models.generate_content(
                model=model_id,
                contents=[dynamic_prompt, image],
                config=gen_config
            )
            
            latency = round(time.time() - start_time, 2)
            act_data = response.parsed
            act = act_data.action.lower().strip()

            # Record action for future turns' memory
            action_history.append(f"Turn Context - Action: {act.upper()}, Details: {act_data.action_description}, Reasoning: {act_data.reasoning}")

            current_sig = f"{act}_{act_data.target_x_norm//30}_{act_data.target_y_norm//30}_{act_data.release_x_norm//30}_{act_data.release_y_norm//30}"
            
            if current_sig == last_action_signature:
                stuck_counter += 1
            else:
                stuck_counter = 0
            last_action_signature = current_sig

            if stuck_counter >= 4:
                print("🚨 [RECOVERY]: Loop detected! Performing right-click reset.")
                pyautogui.rightClick(screen_w // 2, screen_h // 2)
                time.sleep(0.5)

            status_display = "● AUTOPILOT ACTIVE" if stuck_counter < 2 else f"● STUCK DETECTED ({stuck_counter}x)"
            status_col = "#00FF00" if stuck_counter < 2 else "#FF3330"
            act_display = f"Action: {act.upper()} [{latency}s]"
            
            if hud:
                hud.update_hud(status_display, act_display, act_data.reasoning, status_col)

            print("\n" + "="*60)
            print(f"⏱️  Latency:   {latency}s | Stuck Count: {stuck_counter}")
            print(f"🧠  Meta Focus: {act_data.intended_meta_strategy}")
            print(f"🧠  Reasoning: {act_data.reasoning}")
            print(f"🕹️  Action:    {act.upper()} | Details: {act_data.action_description}")
            print(f"🎯  Coords:    Target: ({act_data.target_x_norm}, {act_data.target_y_norm}) | Release: ({act_data.release_x_norm}, {act_data.release_y_norm})")
            print("="*60 + "\n")

            if act == "wait":
                time.sleep(1.0)
                continue
                
            elif act == "end_turn":
                pyautogui.press('e')
                park_mouse()
                time.sleep(1.0)
                continue

            abs_tx = int((act_data.target_x_norm / 1000) * screen_w)
            abs_ty = int((act_data.target_y_norm / 1000) * screen_h)
            
            if act == "click":
                pyautogui.moveTo(abs_tx, abs_ty, duration=0.1)
                
                # Hold the click briefly so the game engine registers it
                pyautogui.mouseDown(button='left')
                time.sleep(0.15) 
                pyautogui.mouseUp(button='left')
                
                # Wait a moment before moving the cursor away so the UI resolves
                time.sleep(0.1)
                park_mouse()
                time.sleep(0.5)  
                
            elif act == "play_card":
                abs_rx = int((act_data.release_x_norm / 1000) * screen_w)
                abs_ry = int((act_data.release_y_norm / 1000) * screen_h)
                
                if abs_rx == 0: abs_rx = screen_w // 2
                if abs_ry == 0: abs_ry = screen_h // 3

                pyautogui.moveTo(abs_tx, abs_ty, duration=0.1)
                pyautogui.mouseDown(button='left')
                time.sleep(0.15)
                
                pyautogui.moveTo(abs_rx, abs_ry, duration=0.2)
                time.sleep(0.2)
                
                pyautogui.mouseUp(button='left')
                time.sleep(0.3)
                
                park_mouse()
                time.sleep(1.0)

            # Hard API throttle to respect rate limits
            time.sleep(3.5)

        except Exception as e:
            err_msg = str(e)
            if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                print("⚠️  [RATE LIMIT] API Quota Exceeded (429). Pausing for 10 seconds to cool down...")
                if hud:
                    hud.update_hud("● RATE LIMITED (429)", "Cooling down...", "Quota exceeded. Waiting 10s...", "#FFCC00")
                time.sleep(10.0)
            elif "timeout" in err_msg.lower():
                print("⏳ [WARNING] API Timeout! Gemini took longer than 12 seconds. Retrying...")
                if hud:
                    hud.update_hud("● API TIMEOUT", "Retrying...", "Gemini response dropped. Re-uplinking...", "#FFCC00")
                time.sleep(1.0)
            else:
                print(f"⚠️  [ERROR] Loop execution failed: {e}")
                if hud:
                    hud.update_hud("● ERROR", "Execution Exception", err_msg, "#FF0000")
                time.sleep(1.5)

if __name__ == "__main__":
    print(f"""
    ==================================================
    🤖 Slay the Spire AI Copilot Initialized
    ==================================================
    Status: STANDBY
    Toggle: Press [{HOTKEY.upper()}] to engage/disengage AI.
    Abort : Move your mouse to the corner of the screen.
    ==================================================
    """)
    keyboard.add_hotkey(HOTKEY, toggle_ai)
    
    hud = OverlayHUD()
    
    ai_thread = threading.Thread(target=ai_loop, daemon=True)
    ai_thread.start()
    
    hud.root.mainloop()