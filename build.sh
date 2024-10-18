BUILD_DIR="build/sphinx"
[[ -d $BUILD_DIR ]] && rm -rf $BUILD_DIR
[[ ! -d venv ]] && python3 -m venv venv
source venv/bin/activate
python3 -m pip install -e .
export VOYAGE_API_KEY=$(cat VOYAGE_API_KEY)
export GEMINI_API_KEY=$(cat GEMINI_API_KEY)
sphinx-build -M html ./doc $BUILD_DIR
deactivate
