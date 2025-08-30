import json
import os

KEYS_FILE = "activation_keys.json"

def fix_keys_file():
    if not os.path.exists(KEYS_FILE):
        print("No keys file found. Creating a new one.")
        with open(KEYS_FILE, 'w') as f:
            json.dump({}, f)
        return
    
    try:
        with open(KEYS_FILE, 'r') as f:
            data = json.load(f)
        
        # Check if the data structure is correct
        needs_fix = False
        for key, value in data.items():
            if not isinstance(value, dict):
                print(f"Found invalid entry: {key} -> {value}")
                needs_fix = True
                break
        
        if needs_fix:
            print("Fixing keys file...")
            # Create a new valid structure
            new_data = {}
            for key, value in data.items():
                if isinstance(value, dict):
                    new_data[key] = value
                else:
                    # Try to create a valid entry from the invalid data
                    new_data[key] = {
                        'user_id': str(value) if isinstance(value, int) else "unknown",
                        'username': "unknown",
                        'discriminator': "0000",
                        'creation_date': "2023-01-01 00:00:00",
                        'active': False,
                        'discord_id': "unknown",
                        'guild_id': "unknown"
                    }
            
            with open(KEYS_FILE, 'w') as f:
                json.dump(new_data, f, indent=4)
            print("Keys file fixed successfully!")
        else:
            print("Keys file structure is correct.")
            
    except Exception as e:
        print(f"Error reading keys file: {e}")
        # Create a new empty file
        with open(KEYS_FILE, 'w') as f:
            json.dump({}, f)
        print("Created a new empty keys file.")
def safe_load_keys():
    """Safely load keys with error handling"""
    try:
        with open(KEYS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        # Ensure all values are dictionaries
        cleaned_data = {}
        for key, value in data.items():
            if isinstance(value, dict):
                cleaned_data[key] = value
            else:
                # Convert invalid entries to valid ones
                cleaned_data[key] = {
                    'user_id': str(value) if isinstance(value, int) else "unknown",
                    'username': "unknown",
                    'discriminator': "0000",
                    'creation_date': str(datetime.now()),
                    'active': False,
                    'discord_id': "unknown",
                    'guild_id': "unknown"
                }
                
        return cleaned_data
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


if __name__ == "__main__":
    fix_keys_file()