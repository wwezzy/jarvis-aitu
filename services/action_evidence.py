"""Conservative persistence checks; model suggestions are never user evidence."""
import copy
import re
from datetime import datetime


def supported_actions(actions, user_text):
    result = copy.deepcopy(actions)
    result['system_command'] = None
    for task in result.get('assignments') or []:
        quote = task.get('deadline_evidence') or ''
        due = task.get('due_at')
        valid = bool(quote and quote in user_text and not re.search(r'вроде|кажется|примерно|maybe|probably|soon|скоро', quote, re.I))
        try:
            value = datetime.fromisoformat(due) if due else None
            valid = valid and value is not None and bool(re.search(rf'(?<!\d){value.hour:02d}:{value.minute:02d}(?!\d)', quote))
        except (ValueError, TypeError):
            valid = False
        if not valid:
            task['due_at'] = None
    workout = result.get('workout_log')
    if workout:
        quote = workout.get('performed_evidence') or ''
        if not quote or quote not in user_text or re.search(r'планир|собираюсь|буду|хочу|planned|will|want to', quote, re.I):
            result['workout_log'] = None
    return result
