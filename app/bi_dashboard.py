import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
exec(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'streamlit_app.py')).read())
