import PyInstaller.__main__
import os
import shutil

# Clean up previous builds
if os.path.exists('build'):
    shutil.rmtree('build')
if os.path.exists('dist'):
    shutil.rmtree('dist')

# Run PyInstaller
PyInstaller.__main__.run([
    'main.py',
    '--name=FundingHunter',
    '--onefile',
    '--noconsole',
    '--clean',
    # Add hidden imports if necessary
    # '--hidden-import=aiohttp',
    # '--hidden-import=tkinter', 
])

print("Build complete. executable should be in dist/FundingHunter.exe")
