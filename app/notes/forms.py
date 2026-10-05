from datetime import date

from flask_wtf import FlaskForm
from wtforms import DateField, TextAreaField
from wtforms.validators import DataRequired, Length


class NoteForm(FlaskForm):
    """Lines are posted as parallel arrays (line_description[], ...), same
    as the invoicing form - prefilled from the original invoice's lines
    and editable, so a partial return/correction is just as easy as a
    full one."""

    note_date = DateField("Note date", validators=[DataRequired()], default=date.today)
    reason = TextAreaField(
        "Reason", validators=[DataRequired(), Length(max=500)]
    )
