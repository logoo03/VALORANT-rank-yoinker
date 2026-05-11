print("If the user runs this with PyQt5 GUI and without Valorant running, it hangs because `while try_again:` blocks the main thread in `Requests.__init__()`. Wait, how was this handled in the CLI?")
print("In the CLI, the `time.sleep` would just pause the terminal. In the GUI, blocking the main thread prevents the window from ever showing up!")
print("We need to move the instantiation of `Requests`, or the logic that blocks, into the `WorkerThread`!")
