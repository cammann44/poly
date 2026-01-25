module.exports = {
  apps: [
    {
      name: 'poly-tracker',
      script: 'track_multi_wallets.py',
      cwd: '/home/ybrid22/projects/hybrid/poly/scripts',
      interpreter: 'python3',
      watch: false,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      log_file: '/home/ybrid22/projects/hybrid/poly/logs/pm2-tracker.log',
      error_file: '/home/ybrid22/projects/hybrid/poly/logs/pm2-tracker-error.log',
      out_file: '/home/ybrid22/projects/hybrid/poly/logs/pm2-tracker-out.log',
      env: {
        PYTHONUNBUFFERED: '1'
      }
    }
  ]
};
