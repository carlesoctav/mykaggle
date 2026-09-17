from typing import Any, List, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.styles import Style
from pygments.lexers.python import PythonLexer
from rich.console import Console

from mykaggle.runtime import KaggleRuntime
from mykaggle.utils import print_output


class KaggleREPL:
    def __init__(
        self,
        runtime: KaggleRuntime,
        output_image: Optional[str] = None,
    ):
        self.runtime = runtime
        self.output_image = output_image
        self.kb = KeyBindings()
        self.console = Console()
        self.repl_history: List[dict] = []

        @self.kb.add("enter")
        def _(event):
            event.current_buffer.validate_and_handle()

        @self.kb.add("escape", "enter")
        @self.kb.add("c-j")
        def _(event):
            event.current_buffer.insert_text("\n")

        self.session = PromptSession(
            history=InMemoryHistory(),
            lexer=PygmentsLexer(PythonLexer),
            include_default_pygments_style=False,
            key_bindings=self.kb,
            multiline=True,
        )
        self.style = Style.from_dict(
            {
                "prompt": "bold blue",
                "continuation": "#888888",
            }
        )

    def print_info(self, message: str):
        self.console.print(f"[bold blue][*][/bold blue] {message}")

    def print_error(self, message: str):
        self.console.print(f"[bold red][!][/bold red] {message}")

    def display_output(self, output: dict):
        print_output(output, self.console, self.output_image)

    def execute(self, code: str):
        try:
            outputs = self.runtime.execute_code(
                code, output_hook=lambda o: self.display_output(o)
            )
            # Ensure next prompt starts on a newline after streaming
            print()

            self.repl_history.append({"input": code, "outputs": outputs or []})
        except Exception as e:
            self.print_error(f"Execution failed: {e}")

    def run(self):
        self.console.print("Python 3 (Kaggle Runtime)\nType /quit to exit.")

        while True:
            try:
                result = self.session.prompt(
                    ">>> ",
                    style=self.style,
                )

                if result is None:
                    continue

                code = result.strip()

                if not code:
                    continue

                if code.lower() in ("/quit", "quit()", "exit()"):
                    break

                self.execute(code)

            except EOFError:
                break
            except KeyboardInterrupt:
                print()
                continue
            except Exception as e:
                self.print_error(f"REPL Error: {e}")

        self.print_info("Goodbye!")
        self.runtime.stop()
