"""Typed, reusable run settings. Profiles never store the main agent command."""
import math
import re
import shlex

BOOLEAN_OPTIONS = ('python_hooks', 'node_hooks', 'protect_files', 'rollback_on_failure')
KEYS = set(BOOLEAN_OPTIONS) | {'verify', 'verify_timeout'}


def valid_name(name):
    return isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', name) is not None


def validate(profiles):
    if not isinstance(profiles, dict):
        raise ValueError('profiles must be a TOML table')
    for name, options in profiles.items():
        if not valid_name(name):
            raise ValueError('Profile names must contain only letters, numbers, underscores or hyphens')
        if not isinstance(options, dict):
            raise ValueError(f'profiles.{name} must be a TOML table')
        if options.keys() - KEYS:
            raise ValueError(f'profiles.{name} contains unsupported options')
        for key in BOOLEAN_OPTIONS:
            if key in options and type(options[key]) is not bool:
                raise ValueError(f'profiles.{name}.{key} must be a boolean')
        commands = options.get('verify', [])
        if not isinstance(commands, list) or any(not isinstance(c, str) for c in commands):
            raise ValueError(f'profiles.{name}.verify must be an array of command strings')
        for index, command in enumerate(commands, 1):
            try:
                argv = shlex.split(command)
                if not argv or not argv[0]:
                    raise ValueError('empty command')
            except ValueError as exc:
                raise ValueError(f'profiles.{name}.verify command {index} is empty or has invalid quoting') from exc
        timeout = options.get('verify_timeout')
        if timeout is not None and (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError(f'profiles.{name}.verify_timeout must be finite and positive')


def resolve(settings, name, overrides):
    available = settings.get('profiles', {})
    if name is not None and name not in available:
        raise ValueError('Unknown run profile: ' + name)
    options = {key: False for key in BOOLEAN_OPTIONS}
    options.update(verify=[], verify_timeout=None)
    if name is not None:
        options.update(available[name])
    options.update({key: value for key, value in overrides.items() if value is not None})
    # Avoid sharing mutable verification lists with the loaded configuration.
    options['verify'] = list(options['verify'])
    return options


def summary(name, options):
    return {'name': name, **{key: options[key] for key in BOOLEAN_OPTIONS},
            'verification_count': len(options['verify']), 'verify_timeout': options['verify_timeout']}
