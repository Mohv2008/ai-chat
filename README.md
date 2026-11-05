# AI Chat for Fenn

![Version](https://img.shields.io/badge/version-1.3.1-blue)
![Python](https://img.shields.io/badge/python-3.8%2B-3776AB)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![Platform](https://img.shields.io/badge/platform-Windows-0078D6)

Chat with an AI from your notch. A plugin for [Fenn](https://fenn.click) that works with your own API key (OpenAI, Anthropic, Gemini, or any OpenAI-compatible service) or a local model (Ollama, LM Studio).

## Features

- **Six providers:** OpenAI, Anthropic, Gemini, Ollama, LM Studio, and Custom (OpenRouter, Groq, DeepSeek, or any OpenAI-compatible URL).
- **Bring your own key or run locally.** No account with this plugin and no middleman.
- **Streaming replies** with a stop button, regenerate, and copy-last-reply.
- **File attachments:** PDFs, images, Word `.docx`, and text or code files (up to 5 per message).
- **Per-provider settings.** Switch between a cloud key and a local model without re-entering anything.
- **Model detection and connection test** built into Settings.
- **Local chat history** with a full-reply view for long answers.
- **Standard library only.** Nothing to install.

## Install

1. Download this repository (**Code > Download ZIP**) or clone it.
2. Copy the folder to:

   ```
   %APPDATA%\Fenn\plugins\ai-chat
   ```

   Or clone straight into place from PowerShell (replace the URL with this repo's):

   ```powershell
   git clone <this-repo-url> "$env:APPDATA\Fenn\plugins\ai-chat"
   ```

3. Open the tile, or press `Ctrl+Alt+A`, and click the gear icon to configure a provider.

### Requirements

- Windows with [Fenn](https://fenn.click) installed
- Python 3.8 or newer
- Optional: `pip install pypdf` for better PDF text extraction with local models

## Setup

Pick a provider in Settings and fill it in:

| Provider | What you need |
|---|---|
| OpenAI | API key from [platform.openai.com](https://platform.openai.com) |
| Anthropic | API key from [console.anthropic.com](https://console.anthropic.com) |
| Gemini | API key from [aistudio.google.com](https://aistudio.google.com) |
| Ollama (local) | Ollama running and a model pulled, e.g. `ollama pull llama3.2`. No key. |
| LM Studio (local) | Local server started with a model loaded. No key. |
| Custom | Any OpenAI-compatible base URL. Key optional. |

Then press **Detect models** (optional), **Test**, and **Save**.

The first reply from a local model can be slow while it loads into memory. **Test** shows how long it has been waiting.

## Usage

| Action | How |
|---|---|
| Open | `Ctrl+Alt+A` or click the tile |
| Send | Enter or the send button |
| Stop a reply | Stop button |
| Read a long reply | **Full reply** button above it (the chat shows the end of long replies) |
| Earlier messages | Clock icon in the header |
| Regenerate | Circular arrow in the header |
| Copy last reply | Copy icon in the header |
| Attach files | **+** next to the text box, or drag files onto the notch |
| New chat / clear history | Pencil icon in the header |

## Attachments

Attach up to 5 files per message. They show as chips above the box; press the **x** on a chip to remove one.

| File | Cloud (OpenAI, Anthropic, Gemini) | Local / Custom (Ollama, LM Studio, ...) |
|---|---|---|
| PDF (up to 10 MB) | Sent as the PDF itself | Text is extracted and sent |
| Image: png, jpg, gif, webp (up to 5 MB) | Sent as an image | Sent as an image (needs a vision model) |
| Word `.docx` (up to 20 MB) | Text is extracted and sent | Same |
| Text or code (up to 2 MB) | Sent as text | Same |

Notes:

- Old `.doc` files aren't supported. Save them as `.docx`.
- Extracted text is cut at about 30,000 characters per message for local models and 200,000 for cloud models. Raise the context length in Ollama or LM Studio if you attach long files.
- Without `pypdf` the plugin uses a basic built-in PDF reader. It handles ordinary text PDFs but not scanned pages or unusual fonts, and tells you when it can't read one.
- Sending with only files attached uses the message "Please look at the attached file(s)."

## Privacy

- **Where messages go:** the last 20 messages of the current chat go only to the provider you selected, at the URL shown in Settings. Nothing goes anywhere else.
- **Attachments:** a file travels with the message that carries it and is sent again with later messages until the chat moves past it. Copies are kept in `%APPDATA%\Fenn\plugin-data\ai-chat\attachments` and deleted when they leave the history or you start a new chat.
- **Local models:** with Ollama or LM Studio, everything stays on your PC.
- **API keys:** encrypted with Windows DPAPI (tied to your Windows user) and stored in `settings.json` in the plugin data folder. They are never shown again after saving. If Windows can't encrypt a key, it isn't saved. Anyone who can run programs as your Windows user can still decrypt it, which is how DPAPI works.
- **Network safety:** only http(s) URLs are used, redirects are refused (so a key can't be forwarded to another host), and an API key is never sent over plain `http://` unless the server is on this PC or your home network.
- **Chat history** (`history.json`) and attachment copies are stored unencrypted in the plugin data folder, so anyone with access to your Windows account can read them. A tampered `history.json` is checked on load and can't make the plugin read files outside that folder.

## Troubleshooting

| Problem | Try this |
|---|---|
| Ollama or LM Studio not found | Make sure the app is running and the base URL in Settings matches (`http://localhost:11434/v1` for Ollama, `http://localhost:1234/v1` for LM Studio). |
| No models listed | Pull or load a model first, then press **Detect models**. |
| Image attachment ignored by a local model | Use a model with vision support. |
| PDF text is empty | The PDF is probably scanned. Install `pypdf`, or use a cloud provider that reads PDFs directly. |
| Key won't save | Windows couldn't encrypt it with DPAPI. Check that you're running as a normal Windows user. |

## Project layout

```
ai-chat/
├── main.py        # the whole plugin (UI, providers, attachments, storage)
├── plugin.toml    # Fenn manifest: id, hotkey, window size, entry point
├── README.md
├── LICENSE
└── .gitignore
```

## License

[MIT](LICENSE)
