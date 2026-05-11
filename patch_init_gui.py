with open("main.py", "r") as f:
    content = f.read()

start_idx = content.find("acc_manager = AccountManager")
end_idx = content.find("class WorkerThread(QThread):")

init_block = content[start_idx:end_idx]

# Replace the block with nothing in global scope
content = content[:start_idx] + "\n# Moved to WorkerThread\n\n\n" + content[end_idx:]

run_start = content.find("    def run(self):\n        global server, Wss, Requests")

# We need to expose table back to main scope or wait to bind it.
# Wait, we can pass the table's html update directly. In WorkerThread, after `table = Table(...)`, we can bind it.
# Actually, WorkerThread has `update_html = pyqtSignal(str)`. We can just emit that signal from inside `WorkerThread.run()`.
# Wait, `table.display()` is called inside `WorkerThread.run()`. We already have `self.update_html.emit(table.html_output)` right after `table.display()`. So we don't need `table.ui_update_callback` anymore!

# Let's indent init_block
init_block_indented = ""
for line in init_block.split("\n"):
    init_block_indented += "        " + line + "\n"

# Remove the line `table.ui_update_callback = self.worker.update_html.emit` from MainWindow since we handle it in run.
content = content.replace("        # Attach callback to table to update GUI\n        table.ui_update_callback = self.worker.update_html.emit\n", "")

run_body_idx = content.find("        firstTime = True", run_start)

# We define some globals just in case
globals_def = "        global table, cfg, content, rank, pstats, presences, menu, coregame, current_map, colors, loadoutsClass, rpc, Wss, valoApiSkins, seasonID, previousSeasonID, gamemodes, agent_dict, map_urls, pregame, namesClass, richConsole\n"

content = content[:run_body_idx] + globals_def + init_block_indented + content[run_body_idx:]

with open("main.py", "w") as f:
    f.write(content)
print("Success")
