"""
Integration test for FileInfoDialog
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from PyQt6.QtWidgets import QApplication
from ui.file_info_dialog import FileInfoDialog

def test_file_info_dialog():
    """Test the file info dialog UI."""
    app = QApplication(sys.argv)
    
    config = {
        "general": {
            "download_directory": r"D:\idm down",
        },
        "network": {
            "proxy": {
                "enabled": False,
            }
        }
    }
    
    # Test with a real URL
    dialog = FileInfoDialog(
        url="https://www.google.com/images/branding/one-google.png",
        filename="one-google.png",
        save_dir=r"D:\idm down\Image",
        config=config,
    )
    
    # Connect signals for testing
    def on_accepted(data):
        print(f"Download accepted: {data}")
        sys.exit(0)
    
    dialog.download_accepted.connect(on_accepted)
    dialog.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    test_file_info_dialog()
