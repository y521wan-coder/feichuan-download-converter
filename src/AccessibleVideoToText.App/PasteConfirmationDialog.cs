using AccessibleVideoToText.Core;

namespace AccessibleVideoToText.App;

public sealed record PasteDecision(bool ConvertVideos, bool TranscribeVideos, bool TranscribeMp3, bool FirstItemOnly)
{
    public string Describe()
    {
        var scope = FirstItemOnly ? "仅处理第一项" : "处理全部";
        if (ConvertVideos && TranscribeVideos && TranscribeMp3)
        {
            return $"已确认{scope}：视频转 MP3 并生成 TXT，同时识别现有 MP3。";
        }

        if (ConvertVideos && TranscribeVideos)
        {
            return $"已确认{scope}：视频转 MP3 并生成 TXT。";
        }

        if (ConvertVideos && TranscribeMp3)
        {
            return $"已确认{scope}：视频仅转 MP3，同时识别现有 MP3。";
        }

        if (ConvertVideos)
        {
            return $"已确认{scope}：视频仅转换 MP3。";
        }

        return $"已确认{scope}：现有 MP3 直接生成 TXT，不重新编码原 MP3。";
    }
}

public sealed class PasteConfirmationDialog : Form
{
    private readonly RadioButton convertOnly = new();
    private readonly RadioButton convertAndTranscribe = new();
    private readonly CheckBox transcribeExistingMp3 = new();
    private readonly Button primaryButton = new();
    private readonly Button firstOnlyButton = new();
    private readonly bool hasVideos;
    private readonly bool hasMp3;

    public PasteConfirmationDialog(IReadOnlyCollection<QueueItem> items)
    {
        ArgumentNullException.ThrowIfNull(items);
        hasVideos = items.Any(item => item.Kind != MediaKind.Mp3);
        hasMp3 = items.Any(item => item.Kind == MediaKind.Mp3);
        var videoCount = items.Count(item => item.Kind != MediaKind.Mp3);
        var mp3Count = items.Count(item => item.Kind == MediaKind.Mp3);

        Text = "确认处理方式";
        AccessibleName = "确认处理方式";
        StartPosition = FormStartPosition.CenterParent;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MinimizeBox = false;
        MaximizeBox = false;
        ShowInTaskbar = false;
        AutoSize = true;
        AutoSizeMode = AutoSizeMode.GrowAndShrink;
        AutoScaleMode = AutoScaleMode.Dpi;
        Padding = new Padding(12);

        var layout = new TableLayoutPanel
        {
            AutoSize = true,
            ColumnCount = 1,
            RowCount = 4,
            Dock = DockStyle.Fill
        };

        var summary = new Label
        {
            AutoSize = true,
            MaximumSize = new Size(560, 0),
            Text = $"本次包含视频或待探测媒体 {videoCount} 个，MP3 {mp3Count} 个。请选择处理方式。",
            AccessibleName = "粘贴文件汇总"
        };
        layout.Controls.Add(summary);

        if (hasVideos)
        {
            var videoGroup = new GroupBox
            {
                Text = "视频处理方式",
                AutoSize = true,
                Dock = DockStyle.Fill
            };
            var videoOptions = new FlowLayoutPanel
            {
                AutoSize = true,
                FlowDirection = FlowDirection.TopDown,
                Dock = DockStyle.Fill,
                Padding = new Padding(8)
            };
            convertOnly.Text = "仅转换 MP3";
            convertOnly.AutoSize = true;
            convertOnly.Checked = true;
            convertAndTranscribe.Text = "转换 MP3 并用腾讯云生成 TXT";
            convertAndTranscribe.AutoSize = true;
            videoOptions.Controls.AddRange([convertOnly, convertAndTranscribe]);
            videoGroup.Controls.Add(videoOptions);
            layout.Controls.Add(videoGroup);
        }

        if (hasMp3)
        {
            transcribeExistingMp3.Text = "同时识别现有 MP3；不重新编码或修改原 MP3";
            transcribeExistingMp3.AutoSize = true;
            transcribeExistingMp3.Checked = true;
            transcribeExistingMp3.Enabled = hasVideos;
            layout.Controls.Add(transcribeExistingMp3);
        }

        var buttons = new FlowLayoutPanel
        {
            AutoSize = true,
            FlowDirection = FlowDirection.RightToLeft,
            Dock = DockStyle.Fill,
            Margin = new Padding(0, 12, 0, 0)
        };

        var cancel = new Button
        {
            Text = "取消",
            AutoSize = true,
            DialogResult = DialogResult.Cancel
        };
        primaryButton.Text = items.Count > 1 ? "处理全部" : "开始";
        primaryButton.AutoSize = true;
        primaryButton.Click += (_, _) => CommitDecision(firstOnly: false);
        buttons.Controls.Add(cancel);
        buttons.Controls.Add(primaryButton);

        if (items.Count > 1 && hasVideos && hasMp3)
        {
            firstOnlyButton.Text = "仅处理第一个";
            firstOnlyButton.AutoSize = true;
            firstOnlyButton.Click += (_, _) => CommitDecision(firstOnly: true);
            buttons.Controls.Add(firstOnlyButton);
        }

        layout.Controls.Add(buttons);
        Controls.Add(layout);
        AcceptButton = primaryButton;
        CancelButton = cancel;
        Shown += (_, _) => primaryButton.Focus();
    }

    public PasteDecision? Decision { get; private set; }

    private void CommitDecision(bool firstOnly)
    {
        Decision = new PasteDecision(
            ConvertVideos: hasVideos,
            TranscribeVideos: hasVideos && convertAndTranscribe.Checked,
            TranscribeMp3: hasMp3 && (!hasVideos || transcribeExistingMp3.Checked),
            FirstItemOnly: firstOnly);
        DialogResult = DialogResult.OK;
        Close();
    }
}

