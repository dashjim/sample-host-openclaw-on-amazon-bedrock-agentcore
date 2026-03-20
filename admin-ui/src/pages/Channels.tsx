import { useState, useEffect } from 'react';
import {
  Card,
  Row,
  Col,
  Button,
  Input,
  Form,
  Tag,
  Spin,
  Typography,
  message,
  Popconfirm,
  Tooltip,
} from 'antd';
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  CopyOutlined,
  SendOutlined,
  DeleteOutlined,
  SaveOutlined,
} from '@ant-design/icons';
import { get, put, post, del } from '../services/api';

const { Title, Text } = Typography;

interface ChannelInfo {
  name: string;
  configured: boolean;
  webhookUrl: string;
}

interface ChannelsResponse {
  channels: ChannelInfo[];
}

export default function Channels() {
  const [channels, setChannels] = useState<ChannelInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);

  const [telegramForm] = Form.useForm();
  const [slackForm] = Form.useForm();
  const [feishuForm] = Form.useForm();

  const fetchChannels = () => {
    setLoading(true);
    get<ChannelsResponse>('/api/channels')
      .then((data) => setChannels(data.channels))
      .catch(console.error)
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchChannels();
  }, []);

  const copyWebhookUrl = (url: string) => {
    navigator.clipboard.writeText(url);
    message.success('Webhook URL copied');
  };

  const handleSaveTelegram = async (values: { botToken: string }) => {
    setSaving('telegram');
    try {
      await put('/api/channels/telegram', { botToken: values.botToken });
      message.success('Telegram credentials saved');
      fetchChannels();
    } catch {
      message.error('Failed to save Telegram credentials');
    }
    setSaving(null);
  };

  const handleRegisterWebhook = async () => {
    setSaving('telegram-webhook');
    try {
      const result = await post<{ telegramResponse: { ok: boolean } }>(
        '/api/channels/telegram/webhook',
        {}
      );
      if (result.telegramResponse?.ok) {
        message.success('Telegram webhook registered');
      } else {
        message.warning('Telegram API returned an unexpected response');
      }
    } catch {
      message.error('Failed to register Telegram webhook');
    }
    setSaving(null);
  };

  const handleSaveSlack = async (values: {
    botToken: string;
    signingSecret: string;
  }) => {
    setSaving('slack');
    try {
      await put('/api/channels/slack', values);
      message.success('Slack credentials saved');
      fetchChannels();
    } catch {
      message.error('Failed to save Slack credentials');
    }
    setSaving(null);
  };

  const handleSaveFeishu = async (values: {
    appId: string;
    appSecret: string;
    verificationToken: string;
    encryptKey: string;
  }) => {
    setSaving('feishu');
    try {
      await put('/api/channels/feishu', values);
      message.success('Feishu credentials saved');
      fetchChannels();
    } catch {
      message.error('Failed to save Feishu credentials');
    }
    setSaving(null);
  };

  const handleClear = async (channel: string) => {
    setSaving(channel);
    try {
      await del(`/api/channels/${channel}`);
      message.success(`${channel} credentials cleared`);
      fetchChannels();
    } catch {
      message.error(`Failed to clear ${channel} credentials`);
    }
    setSaving(null);
  };

  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: 100 }}>
        <Spin size="large" />
      </div>
    );
  }

  const channelMap: Record<string, ChannelInfo> = {};
  for (const ch of channels) {
    channelMap[ch.name] = ch;
  }

  const renderStatusBadge = (configured: boolean) =>
    configured ? (
      <Tag icon={<CheckCircleOutlined />} color="success">
        Configured
      </Tag>
    ) : (
      <Tag icon={<CloseCircleOutlined />} color="default">
        Not Configured
      </Tag>
    );

  return (
    <div>
      <Title level={4}>Channels</Title>

      <Row gutter={[16, 16]}>
        {/* Telegram */}
        <Col xs={24} lg={8}>
          <Card
            title="Telegram"
            extra={renderStatusBadge(channelMap.telegram?.configured ?? false)}
            hoverable
            onClick={() =>
              setExpanded(expanded === 'telegram' ? null : 'telegram')
            }
          >
            {channelMap.telegram?.webhookUrl && (
              <div style={{ marginBottom: 12 }}>
                <Text type="secondary">Webhook URL:</Text>
                <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                  <Text code style={{ fontSize: 11, flex: 1 }}>
                    {channelMap.telegram.webhookUrl}
                  </Text>
                  <Tooltip title="Copy">
                    <Button
                      size="small"
                      icon={<CopyOutlined />}
                      onClick={(e) => {
                        e.stopPropagation();
                        copyWebhookUrl(channelMap.telegram.webhookUrl);
                      }}
                    />
                  </Tooltip>
                </div>
              </div>
            )}

            {expanded === 'telegram' && (
              <div onClick={(e) => e.stopPropagation()}>
                <Form
                  form={telegramForm}
                  layout="vertical"
                  onFinish={handleSaveTelegram}
                >
                  <Form.Item
                    name="botToken"
                    label="Bot Token"
                    rules={[{ required: true }]}
                  >
                    <Input.Password placeholder="123456:ABC-DEF..." />
                  </Form.Item>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <Button
                      type="primary"
                      htmlType="submit"
                      icon={<SaveOutlined />}
                      loading={saving === 'telegram'}
                    >
                      Save
                    </Button>
                    <Button
                      icon={<SendOutlined />}
                      loading={saving === 'telegram-webhook'}
                      onClick={handleRegisterWebhook}
                    >
                      Register Webhook
                    </Button>
                    <Popconfirm
                      title="Clear Telegram credentials?"
                      onConfirm={() => handleClear('telegram')}
                    >
                      <Button danger icon={<DeleteOutlined />}>
                        Clear
                      </Button>
                    </Popconfirm>
                  </div>
                </Form>
              </div>
            )}
          </Card>
        </Col>

        {/* Slack */}
        <Col xs={24} lg={8}>
          <Card
            title="Slack"
            extra={renderStatusBadge(channelMap.slack?.configured ?? false)}
            hoverable
            onClick={() =>
              setExpanded(expanded === 'slack' ? null : 'slack')
            }
          >
            {channelMap.slack?.webhookUrl && (
              <div style={{ marginBottom: 12 }}>
                <Text type="secondary">Webhook URL:</Text>
                <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                  <Text code style={{ fontSize: 11, flex: 1 }}>
                    {channelMap.slack.webhookUrl}
                  </Text>
                  <Tooltip title="Copy">
                    <Button
                      size="small"
                      icon={<CopyOutlined />}
                      onClick={(e) => {
                        e.stopPropagation();
                        copyWebhookUrl(channelMap.slack.webhookUrl);
                      }}
                    />
                  </Tooltip>
                </div>
              </div>
            )}

            {expanded === 'slack' && (
              <div onClick={(e) => e.stopPropagation()}>
                <Form
                  form={slackForm}
                  layout="vertical"
                  onFinish={handleSaveSlack}
                >
                  <Form.Item
                    name="botToken"
                    label="Bot Token"
                    rules={[{ required: true }]}
                  >
                    <Input.Password placeholder="xoxb-..." />
                  </Form.Item>
                  <Form.Item
                    name="signingSecret"
                    label="Signing Secret"
                    rules={[{ required: true }]}
                  >
                    <Input.Password placeholder="Signing secret" />
                  </Form.Item>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <Button
                      type="primary"
                      htmlType="submit"
                      icon={<SaveOutlined />}
                      loading={saving === 'slack'}
                    >
                      Save
                    </Button>
                    <Popconfirm
                      title="Clear Slack credentials?"
                      onConfirm={() => handleClear('slack')}
                    >
                      <Button danger icon={<DeleteOutlined />}>
                        Clear
                      </Button>
                    </Popconfirm>
                  </div>
                </Form>
                <Text
                  type="secondary"
                  style={{ display: 'block', marginTop: 12, fontSize: 12 }}
                >
                  Copy the webhook URL above and paste it into your Slack app's
                  Event Subscriptions Request URL field.
                </Text>
              </div>
            )}
          </Card>
        </Col>

        {/* Feishu */}
        <Col xs={24} lg={8}>
          <Card
            title="Feishu"
            extra={renderStatusBadge(channelMap.feishu?.configured ?? false)}
            hoverable
            onClick={() =>
              setExpanded(expanded === 'feishu' ? null : 'feishu')
            }
          >
            {channelMap.feishu?.webhookUrl && (
              <div style={{ marginBottom: 12 }}>
                <Text type="secondary">Webhook URL:</Text>
                <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                  <Text code style={{ fontSize: 11, flex: 1 }}>
                    {channelMap.feishu.webhookUrl}
                  </Text>
                  <Tooltip title="Copy">
                    <Button
                      size="small"
                      icon={<CopyOutlined />}
                      onClick={(e) => {
                        e.stopPropagation();
                        copyWebhookUrl(channelMap.feishu.webhookUrl);
                      }}
                    />
                  </Tooltip>
                </div>
              </div>
            )}

            {expanded === 'feishu' && (
              <div onClick={(e) => e.stopPropagation()}>
                <Form
                  form={feishuForm}
                  layout="vertical"
                  onFinish={handleSaveFeishu}
                >
                  <Form.Item
                    name="appId"
                    label="App ID"
                    rules={[{ required: true }]}
                  >
                    <Input placeholder="cli_..." />
                  </Form.Item>
                  <Form.Item
                    name="appSecret"
                    label="App Secret"
                    rules={[{ required: true }]}
                  >
                    <Input.Password placeholder="App secret" />
                  </Form.Item>
                  <Form.Item
                    name="verificationToken"
                    label="Verification Token"
                    rules={[{ required: true }]}
                  >
                    <Input.Password placeholder="Verification token" />
                  </Form.Item>
                  <Form.Item
                    name="encryptKey"
                    label="Encrypt Key"
                    rules={[{ required: true }]}
                  >
                    <Input.Password placeholder="Encrypt key" />
                  </Form.Item>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <Button
                      type="primary"
                      htmlType="submit"
                      icon={<SaveOutlined />}
                      loading={saving === 'feishu'}
                    >
                      Save
                    </Button>
                    <Popconfirm
                      title="Clear Feishu credentials?"
                      onConfirm={() => handleClear('feishu')}
                    >
                      <Button danger icon={<DeleteOutlined />}>
                        Clear
                      </Button>
                    </Popconfirm>
                  </div>
                </Form>
                <Text
                  type="secondary"
                  style={{ display: 'block', marginTop: 12, fontSize: 12 }}
                >
                  Copy the webhook URL above and paste it into your Feishu app's
                  Event Subscriptions Request URL field.
                </Text>
              </div>
            )}
          </Card>
        </Col>
      </Row>
    </div>
  );
}
