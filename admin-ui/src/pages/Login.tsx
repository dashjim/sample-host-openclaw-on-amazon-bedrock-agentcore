import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Form, Input, Button, Card, Alert, Typography } from 'antd';
import { LockOutlined, MailOutlined } from '@ant-design/icons';
import { signIn, completeNewPassword } from '../services/auth';

const { Title } = Typography;

export default function Login() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [needsNewPassword, setNeedsNewPassword] = useState(false);

  const handleLogin = async (values: {
    email: string;
    password: string;
  }) => {
    setLoading(true);
    setError('');
    const result = await signIn(values.email, values.password);
    setLoading(false);

    if (result.needsNewPassword) {
      setNeedsNewPassword(true);
      return;
    }
    if (result.error) {
      setError(result.error);
      return;
    }
    if (result.success) {
      navigate('/');
    }
  };

  const handleNewPassword = async (values: { newPassword: string }) => {
    setLoading(true);
    setError('');
    const result = await completeNewPassword(values.newPassword);
    setLoading(false);

    if (result.error) {
      setError(result.error);
      return;
    }
    if (result.success) {
      navigate('/');
    }
  };

  return (
    <div
      style={{
        display: 'flex',
        justifyContent: 'center',
        alignItems: 'center',
        minHeight: '100vh',
        background: 'inherit',
      }}
    >
      <Card style={{ width: 400 }}>
        <Title level={3} style={{ textAlign: 'center', marginBottom: 24 }}>
          OpenClaw Admin
        </Title>

        {error && (
          <Alert
            message={error}
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
          />
        )}

        {needsNewPassword ? (
          <Form onFinish={handleNewPassword} layout="vertical">
            <Alert
              message="Please set a new password"
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
            />
            <Form.Item
              name="newPassword"
              label="New Password"
              rules={[
                { required: true, message: 'Please enter a new password' },
                { min: 12, message: 'Password must be at least 12 characters' },
              ]}
            >
              <Input.Password
                prefix={<LockOutlined />}
                placeholder="New password (12+ characters)"
              />
            </Form.Item>
            <Form.Item>
              <Button
                type="primary"
                htmlType="submit"
                loading={loading}
                block
              >
                Set Password
              </Button>
            </Form.Item>
          </Form>
        ) : (
          <Form onFinish={handleLogin} layout="vertical">
            <Form.Item
              name="email"
              label="Email"
              rules={[{ required: true, message: 'Please enter your email' }]}
            >
              <Input prefix={<MailOutlined />} placeholder="admin@example.com" />
            </Form.Item>
            <Form.Item
              name="password"
              label="Password"
              rules={[
                { required: true, message: 'Please enter your password' },
              ]}
            >
              <Input.Password
                prefix={<LockOutlined />}
                placeholder="Password"
              />
            </Form.Item>
            <Form.Item>
              <Button
                type="primary"
                htmlType="submit"
                loading={loading}
                block
              >
                Sign In
              </Button>
            </Form.Item>
          </Form>
        )}
      </Card>
    </div>
  );
}
