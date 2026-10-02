"""Compatibility launcher for older shortcuts and imports."""
import sys
import entropia_tracker_ui as application

if __name__ == '__main__':
    application.main()
else:
    sys.modules[__name__] = application
