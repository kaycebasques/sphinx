[[ ! -d venv ]] && python3 -m venv venv
source venv/bin/activate
python3 -m pip install -e .
python3 neighbors.py
deactivate
