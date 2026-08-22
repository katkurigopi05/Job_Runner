"""Seed training data for the Naive Bayes baseline.

**This is synthetic.** It is written to read like recruiter mail, and it is
not recruiter mail. CLAUDE.md §15 makes the same admission about the Gate 6
evaluation set, and it applies with more force here: a classifier is only as
good as what it was fitted on, so an accuracy figure produced from this
measures the *method*, never the owner's inbox.

Replacing it is the intended path. `train_from_corpus()` is the single caller,
so pointing it at exported real mail is a one-line change — and the moment
that happens the Gate 6 number starts meaning something.

Deliberately disjoint from `tests/test_inbox.py::LABELED`, which is the
evaluation set. `test_the_training_corpus_does_not_overlap_the_gate_set`
enforces it. Training and scoring on the same messages would produce a high
number that reflects memorisation.
"""

from __future__ import annotations

from packages.core.enums import Classification

#: (label, text) — subject and body are concatenated at fit time, so a single
#: string per example is enough.
TRAINING_CORPUS: tuple[tuple[Classification, str], ...] = (
    # --- Rejections -------------------------------------------------------
    (Classification.REJECTION, "We have chosen to advance other applicants for this opening."),
    (
        Classification.REJECTION,
        "Thank you for applying. We are pursuing candidates whose background aligns more closely.",
    ),
    (
        Classification.REJECTION,
        "After reviewing your materials we have opted not to continue with your candidacy.",
    ),
    (
        Classification.REJECTION,
        "This requisition has been closed and we are not advancing additional applicants.",
    ),
    (
        Classification.REJECTION,
        "We appreciate the time you invested but will not be extending an invitation.",
    ),
    (
        Classification.REJECTION,
        "Your background is impressive though it does not align with what this team needs today.",
    ),
    (Classification.REJECTION, "We have concluded our search and filled the opening internally."),
    # --- Interviews -------------------------------------------------------
    (
        Classification.INTERVIEW,
        "Do you have time next week for a forty five minute technical conversation?",
    ),
    (
        Classification.INTERVIEW,
        "I would love to set up a chat with our hiring manager. What times suit you?",
    ),
    (Classification.INTERVIEW, "Please pick a slot on my calendar for the first round screen."),
    (
        Classification.INTERVIEW,
        "We would like to invite you to an onsite loop with four engineers.",
    ),
    (Classification.INTERVIEW, "Are you free tuesday or wednesday morning to speak with the team?"),
    (
        Classification.INTERVIEW,
        "Congratulations, your application is moving to the technical assessment stage.",
    ),
    (
        Classification.INTERVIEW,
        "Let us book thirty minutes to discuss the position and your experience.",
    ),
    # --- Offers -----------------------------------------------------------
    (Classification.OFFER, "We are delighted to extend an offer of employment for this position."),
    (
        Classification.OFFER,
        "Attached is your offer letter including base compensation and equity details.",
    ),
    (
        Classification.OFFER,
        "The team voted unanimously and we would like you to join us. Here are the terms.",
    ),
    (Classification.OFFER, "Please review the enclosed package and let us know if you accept."),
    (
        Classification.OFFER,
        "We are prepared to offer the role at the salary we discussed, starting next month.",
    ),
    (
        Classification.OFFER,
        "Welcome aboard pending your signature on the attached employment agreement.",
    ),
    # --- Information requests ---------------------------------------------
    (
        Classification.INFO_REQUEST,
        "Could you send over your availability and current notice period?",
    ),
    (Classification.INFO_REQUEST, "We need two professional references before proceeding further."),
    (Classification.INFO_REQUEST, "Please complete the attached background check consent form."),
    (
        Classification.INFO_REQUEST,
        "Can you confirm your preferred start date and desired compensation range?",
    ),
    (
        Classification.INFO_REQUEST,
        "Kindly upload a copy of your portfolio to the applicant portal.",
    ),
    (
        Classification.INFO_REQUEST,
        "We are missing your signed disclosure. Please return it at your convenience.",
    ),
    # --- Acknowledgements --------------------------------------------------
    (
        Classification.ACKNOWLEDGEMENT,
        "This is an automated confirmation that your submission was received.",
    ),
    (
        Classification.ACKNOWLEDGEMENT,
        "Thanks for applying. Our recruiting team reviews every submission and will be in touch.",
    ),
    (
        Classification.ACKNOWLEDGEMENT,
        "Your profile has been added to our applicant tracking system.",
    ),
    (
        Classification.ACKNOWLEDGEMENT,
        "We got your materials. No action is needed from you right now.",
    ),
    (
        Classification.ACKNOWLEDGEMENT,
        "Application received. Please allow several weeks for review.",
    ),
    # --- One-time passcodes -------------------------------------------------
    (Classification.OTP, "Your verification code is 481920. It expires in ten minutes."),
    (Classification.OTP, "Use security code 733914 to finish signing in to the careers portal."),
    (Classification.OTP, "Enter 204857 to confirm your email address."),
    (Classification.OTP, "Your one time passcode is 918273. Do not share it with anyone."),
    (Classification.OTP, "Confirm your identity with the code 556210 to continue."),
    # --- Noise --------------------------------------------------------------
    (Classification.NOISE, "Join our webinar on engineering leadership trends this quarter."),
    (Classification.NOISE, "Our quarterly newsletter is out, featuring life at the company."),
    (
        Classification.NOISE,
        "You are receiving this because you subscribed to job alerts. Unsubscribe here.",
    ),
    (Classification.NOISE, "Save the date for our annual open house and networking mixer."),
    (Classification.NOISE, "Check out these five tips for building a standout profile."),
    (Classification.NOISE, "Scheduled maintenance will affect the portal this weekend."),
)
