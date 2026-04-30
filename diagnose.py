#!/usr/bin/env python3
"""
Diagnostic script to test FindMy setup and connectivity
"""

import asyncio
import json
import sys
from pathlib import Path

def print_header(text):
    print("\n" + "="*50)
    print(f" {text}")
    print("="*50)

def print_success(text):
    print(f"✓ {text}")

def print_error(text):
    print(f"✗ {text}")

def print_warning(text):
    print(f"⚠ {text}")

async def main():
    print_header("FindMy Diagnostic Tool")
    
    # Add examples to path
    sys.path.insert(0, str(Path(__file__).parent))
    
    # Check files
    print_header("File Checks")
    
    account_exists = Path("account.json").exists()
    device_exists = Path("device.json").exists()
    
    if account_exists:
        print_success("account.json found")
    else:
        print_warning("account.json NOT found - will need to log in")
    
    if device_exists:
        print_success("device.json found")
    else:
        print_error("device.json NOT found")
        return 1
    
    # Load and verify device
    print_header("Device Information")
    
    try:
        with open("device.json") as f:
            device_data = json.load(f)
        
        print_success("device.json is valid JSON")
        print(f"  Name: {device_data.get('name', 'N/A')}")
        print(f"  Model: {device_data.get('model', 'N/A')}")
        print(f"  Identifier: {device_data.get('identifier', 'N/A')}")
        print(f"  Paired: {device_data.get('paired_at', 'N/A')}")
        print(f"  Has master_key: {'master_key' in device_data}")
        print(f"  Has keys: {'skn' in device_data and 'sks' in device_data}")
    except Exception as e:
        print_error(f"Failed to load device.json: {e}")
        return 1
    
    # Load FindMy and test
    print_header("FindMy Library Test")
    
    try:
        from findmy import FindMyAccessory
        print_success("FindMy library loaded")
        
        tracker = FindMyAccessory.from_json("device.json")
        print_success(f"Device loaded: {tracker.name if hasattr(tracker, 'name') else tracker}")
    except Exception as e:
        print_error(f"Failed to load device: {e}")
        return 1
    
    # Test account if it exists
    if not account_exists:
        print_header("Account Status")
        print_warning("account.json not found")
        print("""
To authenticate:
1. Start the web tracker: ./RUN_WEB_TRACKER.sh
2. You will be prompted to log in with your Apple ID
3. Follow the 2FA prompts if needed
""")
        return 0
    
    print_header("Account Authentication")
    
    try:
        from findmy import AsyncAppleAccount
        
        acc = AsyncAppleAccount.from_json(
            "account.json",
            anisette_libs_path="ani_libs.bin"
        )
        print_success("Account loaded")
        print(f"  User: {acc.account_name}")
        print(f"  Name: {acc.first_name} {acc.last_name}")
        
        # Try to fetch location
        print_header("Location Report Test")
        print("Fetching latest report (this may take a moment)...")
        
        try:
            location = await acc.fetch_location(tracker)
            
            if location is None:
                print_warning("No location report available")
                print("""
This could mean:
1. AirTag is offline/not broadcasting
2. No Apple devices detected it
3. Apple servers haven't processed the report yet
4. AirTag is brand new (wait 10-30 minutes for first report)

Try the history endpoint to see if any reports exist:
  curl http://localhost:8080/api/location-history
""")
            else:
                print_success(f"Location found!")
                print(f"  Timestamp: {location.timestamp}")
                print(f"  Lat/Lon: {location.latitude:.4f}, {location.longitude:.4f}")
                print(f"  Accuracy: ±{location.horizontal_accuracy}m")
                print(f"  Confidence: {location.confidence}")
        except Exception as e:
            print_error(f"Failed to fetch location: {e}")
        
        # Fetch history
        print_header("Location History")
        print("Fetching all available reports...")
        
        try:
            history = await acc.fetch_location_history(tracker)
            
            if isinstance(history, list):
                if history:
                    print_success(f"Found {len(history)} reports")
                    print("\nRecent reports:")
                    for i, report in enumerate(sorted(history, key=lambda r: r.timestamp)[-5:]):
                        print(f"  {i+1}. {report.timestamp} - "
                              f"({report.latitude:.4f}, {report.longitude:.4f}) "
                              f"±{report.horizontal_accuracy}m")
                else:
                    print_warning("No reports in history")
            else:
                print(f"History result: {history}")
        except Exception as e:
            print_error(f"Failed to fetch history: {e}")
        
        await acc.close()
        
    except Exception as e:
        print_error(f"Failed to test account: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    print_header("Diagnostic Complete")
    print("""
If everything looks good:
  1. Run: ./RUN_WEB_TRACKER.sh
  2. Open: http://localhost:8080
  3. Wait for map to load location

If there's still an issue, check the TROUBLESHOOTING.md file.
""")
    
    return 0

if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(130)
