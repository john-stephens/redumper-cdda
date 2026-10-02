class RecordingReporter:
    verbose = False

    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)
