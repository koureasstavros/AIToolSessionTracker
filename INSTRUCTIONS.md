To make a new release and push it:

First login with GitHub:
gh auth login

Second set the default directory:
gh repo set-default koureasstavros/AIToolSessionTracker

The generate the compiled binary based on the platform
Run the script into packer/<platform>/*

For generating release based on source code:
gh release create v0.0.XX --notes-file README.md
gh release delete v0.0.XX

For generating release based on source code and a compiled binary:
gh release create v0.0.XX dist/AI-Tool-Session-Explorer.exe --title "v0.0.XX" --notes-file README.md
gh release delete v0.0.XX