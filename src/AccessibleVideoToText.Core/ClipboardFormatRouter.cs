namespace AccessibleVideoToText.Core;

public enum ClipboardPayloadKind
{
    None,
    FileDrop,
    Text
}

public static class ClipboardFormatRouter
{
    public static ClipboardPayloadKind Choose(bool containsFileDrop, bool containsText)
    {
        if (containsFileDrop)
        {
            return ClipboardPayloadKind.FileDrop;
        }

        return containsText ? ClipboardPayloadKind.Text : ClipboardPayloadKind.None;
    }
}
